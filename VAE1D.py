import os, math, time
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd
from tqdm import tqdm

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

from sklearn.metrics import roc_auc_score
from itertools import product
import scipy.stats as stats


depth = 64      # initial depth to convolve channels into
filt_size = 4   # convolution filter size
stride = 2      # stride for conv
pad = 1         # padding added for conv


class VAE1D(nn.Module):
    def __init__(self, size, n_channels, n_latent=100):
        
        # Model setup
        #############
        super(VAE1D, self).__init__()
        self.n_latent = n_latent
        n = math.log2(size)
        assert n == round(n), 'Vector size must be a power of 2'  # restrict input sizes permitted
        assert n >= 3, 'Vector size must be at least 8'           # low dimensional data won't work well
        n = int(n)

        # Encoder - first half of VAE
        #############################
        self.encoder = nn.Sequential()  
        # input: n_channels x size
        # ouput: depth x conv_size
        # conv_size = (size - filt_size + 2 * pad) / stride + 1
        self.encoder.add_module('input-conv', nn.Conv1d(n_channels, depth,
                                                        filt_size, stride, pad,
                                                        bias=True))
        # TODO - add batchnorm?
        self.encoder.add_module('input-relu', nn.ReLU(inplace=True))
        
        # Add conv layer for each power of 2 over 3 (min size)
        # Pyramid strategy with batch normalization added
        for i in range(n - 3):
            # input: i_depth x conv_size
            # output: o_depth x conv_size
            # i_depth = o_depth of previous layer
            i_depth = depth * 2 ** i
            o_depth = depth * 2 ** (i + 1)
            self.encoder.add_module(f'pyramid_{i_depth}-{o_depth}_conv',
                                    nn.Conv1d(i_depth, o_depth, filt_size, stride, pad, bias=True))
            self.encoder.add_module(f'pyramid_{o_depth}_batchnorm',
                                    nn.BatchNorm1d(o_depth))
            self.encoder.add_module(f'pyramid_{o_depth}_relu',
                                    nn.ReLU(inplace=True))
        
        # Latent representation
        #######################
        # Convolve the encoded vector into the latent space, once for mu and once for log variance
        max_depth = depth * 2 ** (n - 3)
        self.conv_mu = nn.Conv1d(max_depth, n_latent, filt_size)
        self.conv_logvar = nn.Conv1d(max_depth, n_latent, filt_size)
        
        
        # Decoder - second half of VAE
        ##############################
        self.decoder = nn.Sequential()
        # input: max_depth x conv_size
        # output: n_latent x conv_size
        # default stride=1, pad=0 for this layer
        self.decoder.add_module('input-conv', nn.ConvTranspose1d(n_latent, max_depth, filt_size, bias=True))
        self.decoder.add_module('input-batchnorm', nn.BatchNorm1d(max_depth))
        self.decoder.add_module('input-relu', nn.ReLU(inplace=True))
    
        # Reverse the convolution pyramids used in the encoder
        for i in range(n - 3, 0, -1):
            i_depth = depth * 2 ** i
            o_depth = depth * 2 ** (i - 1)
            self.decoder.add_module(f'pyramid_{i_depth}-{o_depth}_conv',
                                    nn.ConvTranspose1d(i_depth, o_depth, filt_size, stride, pad, bias=True))
            self.decoder.add_module(f'pyramid_{o_depth}_batchnorm',
                                    nn.BatchNorm1d(o_depth))
            self.decoder.add_module(f'pyramid_{o_depth}_relu', nn.ReLU(inplace=True))
        
        # Final transposed convolution to return to vector size
        # Final activation is tanh instead of relu to allow negative output
        self.decoder.add_module('output-conv', nn.ConvTranspose1d(depth, n_channels,
                                                                  filt_size, stride, pad,
                                                                  bias=True))
        self.decoder.add_module('output-tanh', nn.Tanh())

        # Model weights init
        ####################
        # Randomly initialize the model weights using kaiming method
        # Reference: "Delving deep into rectifiers: Surpassing human-level
        # performance on ImageNet classification" - He, K. et al. (2015)
        for m in self.modules():
            if isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')

    def encode(self, trans):
        """
        Encode time-series vectors (transients) into latent space mean and log variance vectors
        input:  trans  [batch_size, n_channels, size, size]
        output: mu     [batch_size, n_latent, 1, 1]
                logvar [batch_size, n_latent, 1, 1]
        """
        output = self.encoder(trans)
        output = output.squeeze(-1).squeeze(-1)
        return [self.conv_mu(output), self.conv_logvar(output)]

    def generate(self, mu, logvar):
        """
        Generate random latent space vector sampled from the trained normal distributions
        input:  mu     [batch_size, n_latent, 1, 1]
                logvar [batch_size, n_latent, 1, 1]
        output: gen    [batch_size, n_latent, 1, 1]
        """
        std = torch.exp(0.5 * logvar)
        gen = torch.randn_like(std)
        return gen.mul(std).add_(mu)

    def decode(self, gen):
        """
        Restores an transient representation from the generated latent space vector
        input:  gen       [batch_size, n_latent, 1, 1]
        output: gen_trans [batch_size, n_channels, size, size]
        """
        return self.decoder(gen)

    def forward(self, imgs):
        """
        Generates reconstituted images from input images based on learned representation
        input: trans     [batch_size, n_channels, size, size]
        ouput: gen_trans [batch_size, n_channels, size, size]
               mu        [batch_size, n_latent]
               logvar    [batch_size, n_latent]
        """
        mu, logvar = self.encode(trans)
        gen = self.generate(mu, logvar)
        for vecs in (mu, logvar):
            vecs = vecs.squeeze(-1).squeeze(-1)
        return self.decode(gen), mu, logvar


class VAE1DLoss(nn.Module):

    def __init__(self, beta=1):
        super(VAE1DLoss, self).__init__()
        self.beta = beta  # relative weight of the KL term

    def forward(self, gen_trans, trans, mu, logvar, reduce=True):
        """
        input:  gen_trans [batch_size, n_channels, size, size]
                trans     [batch_size, n_channels, size, size]
                mu        [batch_size, n_latent]
                logvar    [batch_size, n_latent]
        output: loss      scalar (-ELBO)
                loss_desc {'KL', 'logp'}
        """
        # Reconstruction loss
        batch_size = trans.shape[0]
        gen_err = (trans - gen_trans).pow(2).reshape(batch_size, -1)
        gen_err = 0.5 * torch.sum(gen_err, dim=-1)
        if reduce:
            gen_err = torch.mean(gen_err)

        # Regularizer
        # KL(q || p) = -log_sigma + sigma^2/2 + mu^2/2 - 1/2
        # K-L divergence of learned pdf to standard gaussian N(0, 1)
        KL = (-logvar + logvar.exp() + mu.pow(2) - 1) * 0.5
        KL = torch.sum(KL, dim=-1)
        if reduce:
            KL = torch.mean(KL)

        loss = gen_err + self.beta * KL
        return loss, {'KL': KL, 'logp': -gen_err}


class TransientDataset(Dataset):
    
    def __init__(self, data_path, normals):
        self.path = Path(data_path)
        # Normals is an array of mean and std for each sensor
        self.mu = normals[:, 0][:, None]  # want arrays, not vectors
        self.sigma = normals[:, 1][:, None]
        
        self.classes = sorted(os.listdir(self.path))
        self.names = []
        self.targets =[]
        for i, c in enumerate(self.classes):
            names = os.listdir(self.path / c)
            targets = [i] * len(names)
            self.names.append(names)
            self.targets.append(targets)
        
    def __len__(self):
        return len(self.names)
    
    def __getitem__(self, index):
        name = self.names[index]
        target = self.targets[index]
        path = self.path / self.classes[target] / name
        return (torch.Tensor(self.norm(np.load(path))), target)
    
    def index(self, name):
        name = str(name)
        if index[-4:] != '.npy':
            index = index + '.npy'
        return self.names.index(index)
    
    def norm(self, arr):
        # Normalize sensor channels to N(0, 1)
        return (arr - self.mu) / self.sigma


def load_datasets(data_path, batch_size=32):
    """
    Load the transient datasets from train and test into dataloaders
    Must have normals for each sensor channel saved to normals.npy
    """
    data_path = Path(data_path)
    train_path = data_path / 'train/train/'
    val_path = data_path / 'train/val/'
    test_path = data_path / 'test/'
    
    normals = np.load('normals.npy')

    train_ds = datasets.TransientDataset(train_path, normals)
    val_ds = datasets.TransientDataset(val_path, normals)
    test_ds = datasets.TransientDataset(test_path, normals)
    
    loader_args = {'shuffle': True,
                   'num_workers': 4}
    train_dl = torch.utils.data.DataLoader(train_ds,
                                           batch_size=batch_size,
                                           **loader_args)
    val_dl = torch.utils.data.DataLoader(val_ds,
                                         batch_size=batch_size,
                                         **loader_args)
    test_dl = torch.utils.data.DataLoader(test_ds,
                                          batch_size=1,
                                          ** loader_args)
    
    return train_dl, val_dl, test_dl