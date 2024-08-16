import skorch.helper
import skorch.scoring
from torch import nn
import torch
import pandas as pd
import numpy as np
import os
import skorch
import scipy
import math
from skorch.hf import AccelerateMixin
from accelerate import Accelerator
from skorch.helper import SliceDict
from sklearn.model_selection import ParameterGrid
import time
import torch.nn.functional as F
import copy


import sys
sys.path.append('../')
from typing import Dict, List, Tuple

from utils.ml_utils import clones, StackedTransformer
from flash_attn import flash_attn_qkvpacked_func
from local_attention import LocalAttention

class SequenceEmbedder(nn.Module):
    def __init__(self, embed_dim: int = 4, sequence_length=99, onehot: bool = True, annot: bool = False):
        super().__init__()
        self.num_nucl = 4 # nucleotide embeddings
        self.onehot = onehot
        self.annot = annot
        # self.num_inidc = 2 # padding index for protospacer, PBS and RTT
        # wt+mut sequence embedding
        if not self.onehot:
            self.We = nn.Embedding(self.num_nucl+1, embed_dim, padding_idx=self.num_nucl)
        # # protospacer embedding
        # self.Wproto = nn.Embedding(self.num_inidc+1, annot_embed, padding_idx=self.num_inidc)
        # # PBS embedding
        # self.Wpbs = nn.Embedding(self.num_inidc+1, annot_embed, padding_idx=self.num_inidc)
        # # RTT embedding
        # self.Wrt = nn.Embedding(self.num_inidc+1, annot_embed, padding_idx=self.num_inidc)
        
        # Create a matrix of shape (max_len, embed_dim) for position encodings
        position_encoding = torch.zeros(sequence_length, embed_dim)
        position = torch.arange(0, sequence_length, dtype=torch.float).unsqueeze(1)
        
        # Compute the division term (10000^(2i/embed_dim))
        # This will be used to compute the sine and cosine functions
        div_term = torch.exp(torch.arange(0, embed_dim, 2).float() * (-math.log(10000.0) / embed_dim))
        
        # Apply sine to even indices and cosine to odd indices
        position_encoding[:, 0::2] = torch.sin(position * div_term)
        position_encoding[:, 1::2] = torch.cos(position * div_term)
        
        # Add an extra batch dimension to the position encoding
        position_encoding = position_encoding.unsqueeze(0)
        
        # Register the position encoding as a buffer, which is a tensor not considered a model parameter
        self.register_buffer('position_encoding', position_encoding)
    
    def forward(self, X_nucl: torch.tensor, X_pbs: torch.tensor=None, X_rtt: torch.tensor=None, padding_mask: torch.tensor=None) -> torch.tensor:
        """forward pass of the sequence embedder

        Args:
            X_nucl (torch.tensor): numerical representation of the nucleotide sequence
            padding_mask (torch.tensor, optional): tensor, float32, (batch, sequence length) representing the padding mask. Defaults to None.

        Returns:
            torch.tensor: tensor, float32, (batch, sequence length, embed_dim) embedded sequence
        """
        # one hot encode the sequence
        x = F.one_hot(X_nucl, num_classes=self.num_nucl)
        if self.annot:
            # add a dimension to the PBS and RTT
            X_pbs = X_pbs.unsqueeze(-1)
            X_rtt = X_rtt.unsqueeze(-1)
            # concatenate the positional information
            x = torch.cat([x, X_pbs, X_rtt], dim=-1)
        
        # if not self.onehot:
        #     x = self.We(X_nucl)
        
        # position embedding for non padding sequence using sinusoidal function
        x = x + self.position_encoding[:, :x.size(1)]
        
        if padding_mask is not None:
            # Expand the mask to match the dimensions of x (seq_len, batch_size, embed_dim)
            # print distinct values in the padding mask
            padding_mask = padding_mask.unsqueeze(-1).expand(x.size())
            x = x.masked_fill(padding_mask, 0)
        
        return x


class MultiHeadAttention(nn.Module):
    def __init__(self, num_heads, embed_dim, pdropout, flash: bool = False, local: bool = False):
        super(MultiHeadAttention, self).__init__()
        # embedding dimension of the sequence must be divisible by the number of heads
        assert embed_dim % num_heads == 0
        self.head_dim = embed_dim // num_heads
        self.num_heads = num_heads
        self.linears = clones(nn.Linear(embed_dim, embed_dim), 4)
        self.dropout = nn.Dropout(pdropout)
        self.attention = attention if not local else LocalAttention(window_size=5, causal=False, look_backward=1, look_forward=0, dropout=pdropout)
        self.attn = None
        self.flash = flash
        self.local = local
    
    def forward(self, query, key, value, mask = None):
        "Implement the scaled dot product attention"
        # query, key, value are all of shape (batch, sequence length, embed_dim)
        if mask is not None:
            # Same mask applied to all heads
            mask = mask.unsqueeze(1)
        batch_size = query.size(0)
        
        # Do all the linear projections in batch from embed_dim => num_heads x head_dim
        # split the embed_dim into num_heads
        # (batch, sequence length, embed_dim) => (batch, sequence length, num_heads, head_dim)
        query, key, value = [l(x).view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
                             for l, x in zip(self.linears, (query, key, value))]
        
        if self.flash:
            # stack the query, key and value
            # (batch, sequence length, num_heads, head_dim) => (batch, num_heads, 3, sequence length, head_dim)
            qkv = torch.stack([query, key, value], dim=-1)
            # permute the dimensions to (batch, sequence length, 3, num_heads, head_dim)
            qkv = qkv.permute(0, 1, 4, 2, 3)
            
            if self.local:
                # feed the input to the flash attention
                x, softmax_lse, self.attn = flash_attn_qkvpacked_func(qkv=qkv, dropout_p=self.dropout.p, return_attn_probs=True, window_size=(2, 2))
            else:
                x, softmax_lse, self.attn = flash_attn_qkvpacked_func(qkv=qkv, dropout_p=self.dropout.p, return_attn_probs=True)
                
            del qkv
            # del softmax_lse
        # elif self.attention != attention: 
        #     # use local attention
        else:
            if self.local:
                # no attention probabilities is returned
                x = self.attention(query, key, value, mask=None)
            else:
                x, self.attn = self.attention(query, key, value, mask=None, dropout=self.dropout)
        del query, key, value

        # concatenate the output of the attention heads into the same shape as the input
        # (batch, sequence length, num_heads, head_dim) => (batch, sequence length, embed_dim)
        x = x.transpose(1, 2).contiguous().view(batch_size, -1, self.num_heads * self.head_dim)
        
        return self.linears[-1](x)
    
    
class PositionwiseFeedForward(nn.Module):
    def __init__(self, embed_dim, mlp_embed_dim, pdropout):
        super(PositionwiseFeedForward, self).__init__()
        self.linear1 = nn.Linear(embed_dim, mlp_embed_dim)
        self.linear2 = nn.Linear(mlp_embed_dim, embed_dim)
        self.dropout = nn.Dropout(pdropout)
    
    def forward(self, x):
        return self.linear2(self.dropout(F.relu(self.linear1(x))))
    

class ResidualConnection(nn.Module):
    """
    A residual connection followed by a layer norm.
    Note for code simplicity the norm is first as opposed to last.
    """

    def __init__(self, size, dropout):
        super(ResidualConnection, self).__init__()
        self.norm = nn.LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        "Apply residual connection to any sublayer with the same size."
        return x + self.dropout(sublayer(self.norm(x)))
    

class Encoder(nn.Module):
    def __init__(self, layer, N):
        super(Encoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = nn.LayerNorm(layer.size)
        
    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)#.half()
    
class EncoderLayer(nn.Module):
    def __init__(self, size, self_attn, feed_forward, dropout):
        super(EncoderLayer, self).__init__()
        # self attention
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(ResidualConnection(size, dropout), 2)
        self.size = size
        
    def forward(self, x, mask):
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        return self.sublayer[1](x, self.feed_forward)
    

class Decoder(nn.Module):
    def __init__(self, layer, N):
        super(Decoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = nn.LayerNorm(layer.size)
        
    def forward(self, x: torch.Tensor, enc_out: torch.Tensor, wt_mask: torch.Tensor, mut_mask: torch.Tensor):
        for layer in self.layers:
            x = layer(x, enc_out, wt_mask, mut_mask)
        return self.norm(x)#.half()

class DecoderLayer(nn.Module):
    def __init__(self, size, self_attn, cross_attn, feed_forward, dropout):
        super(DecoderLayer, self).__init__()
        # self attention
        self.self_attn = self_attn
        # source attention
        self.cross_attn = cross_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(ResidualConnection(size, dropout), 3)
        self.size = size
        
    def forward(self, x, enc_out, wt_mask, mut_mask):
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mut_mask))
        x = self.sublayer[1](x, lambda x: self.cross_attn(x, enc_out, enc_out, wt_mask))
        return self.sublayer[2](x, self.feed_forward)


class Transformer(nn.Module):
    """encoder decoder transformer architecture

    Args:
        nn (_type_): _description_

    Returns:
        _type_: _description_
    """
    def __init__(self, encoder, decoder, wt_embed, mut_embed, onehot=True, annot: bool = False):
        super(Transformer, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.wt_embed = wt_embed
        self.mut_embed = mut_embed
        self.onehot = onehot
        self.annot = annot
        
    def forward(self, X_wt: torch.Tensor, X_mut: torch.Tensor, X_pbs, X_rtt, X_rtt_mut) -> torch.Tensor:
        """forward pass of the transformer

        Args:
            X_wt (torch.Tensor): one dimensional tensor representing the wild type sequence, (batch, sequence length)
            X_mut (torch.Tensor): one dimensional tensor representing the mutated sequence, (batch, sequence length)
            wt_mask (torch.Tensor): mask for the wild type sequence, (batch, sequence length)
            mut_mask (torch.Tensor): mask for the mutated sequence, (batch, sequence length) 

        Returns:
            _type_: _description_
        """
        padding_mask_wt = (X_wt == 4)
        padding_mask_mut = (X_mut == 4)
        
        if self.onehot:
            # convert the padding value to 0 so that one hot encoding can be applied
            X_wt = X_wt.masked_fill(padding_mask_wt, 0)
            X_mut = X_mut.masked_fill(padding_mask_mut, 0)
        
        # convert the sequence to embeddings
        # (batch, sequence length, embed_dim)
        if self.annot:
            wt_embed = self.wt_embed(X_nucl=X_wt, padding_mask = padding_mask_wt, X_pbs=X_pbs, X_rtt=X_rtt)
            mut_embed = self.mut_embed(X_nucl=X_mut, padding_mask = padding_mask_mut, X_pbs=X_pbs, X_rtt=X_rtt_mut) 
        else:
            wt_embed = self.wt_embed(X_wt, padding_mask = padding_mask_wt)
            mut_embed = self.mut_embed(X_mut, padding_mask = padding_mask_mut) 
        
        # transform to half precision for faster computation
        wt_embed = wt_embed#.half()
        mut_embed = mut_embed#.half()
        
        padding_mask_wt = padding_mask_wt#.half()
        padding_mask_mut = padding_mask_mut#.half()
        
        # print('wt_embed:', wt_embed.dtype)
        
        # wt_seq and mut_seq are alread masked
        enc_out = self.encoder(wt_embed, padding_mask_wt)
        dec_out = self.decoder(mut_embed, enc_out, padding_mask_wt, padding_mask_mut)
        
        return dec_out

def make_model(N=6, embed_dim=4, mlp_embed_dim=64, num_heads=4, pdropout=0.1, onehot=True, annot=False, flash=False, local=False):
    c = copy.deepcopy
    attn = MultiHeadAttention(num_heads, embed_dim, pdropout, flash=flash)
    attn_local = MultiHeadAttention(num_heads, embed_dim, pdropout, local=True, flash=flash) if local else attn
    position_ff = PositionwiseFeedForward(embed_dim, mlp_embed_dim, pdropout)
    model = Transformer(
        encoder=Encoder(EncoderLayer(embed_dim, c(attn_local), c(position_ff), pdropout), N),
        decoder=Decoder(DecoderLayer(embed_dim, c(attn_local), c(attn), c(position_ff), pdropout), N),
        wt_embed=SequenceEmbedder(embed_dim=embed_dim, onehot=onehot, annot=annot),
        mut_embed=SequenceEmbedder(embed_dim=embed_dim, onehot=onehot, annot=annot),
        annot=annot,
        onehot=onehot
    )
    
    # initialize the parameters with xavier uniform
    for p in model.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)
    
    return model

class PrimeDesignTransformer(nn.Module):
    def __init__(self, embed_dim: int = 4, sequence_length=99, num_heads=4, pdropout=0.1, mlp_embed_dim=64, nonlin_func=nn.ReLU(), num_encoder_units=2, num_features=24, flash=True, onehot=True, annot=False, local=False):
        super(PrimeDesignTransformer, self).__init__()
        self.embed_dim = embed_dim
        self.sequence_length = sequence_length
        self.num_heads = num_heads
        self.pdropout = pdropout
        self.mlp_embed_dim = mlp_embed_dim
        self.nonlin_func = nonlin_func
        self.num_encoder_units = num_encoder_units
        self.num_features = num_features
        self.flash = flash
        self.onehot = onehot
        
        self.transformer = make_model(N=num_encoder_units, embed_dim=embed_dim, mlp_embed_dim=mlp_embed_dim, num_heads=num_heads, pdropout=pdropout, onehot=onehot, annot=annot, flash=flash, local=local)
        self.linear_transformer = nn.Linear(embed_dim, 1)
        # self.gru = nn.GRU(input_size=sequence_length, hidden_size=128, num_layers=1, batch_first=True, bidirectional=True)
        
        self.d = nn.Sequential(
            nn.Linear(num_features, 96, bias=False),
            nn.ReLU(),
            nn.Dropout(pdropout),
            nn.Linear(96, 64, bias=False),
            nn.ReLU(),
            nn.Dropout(pdropout),
            nn.Linear(64, 128, bias=False)
        )

        self.head = nn.Sequential(
            nn.LayerNorm(sequence_length + 128),
            nn.Dropout(pdropout),
            nn.Linear(sequence_length + 128, 1, bias=True),
        )
        
    def forward(self, X_nucl: torch.Tensor, X_mut_nucl: torch.Tensor, X_pbs, X_rtt, X_rtt_mut, features: torch.Tensor) -> torch.Tensor:
        """forward pass of the transformer model

        Args:
            X_nucl (torch.Tensor): tensor, float32, (batch, sequence length) representing the wild type sequence
            X_mut_nucl (torch.Tensor): tensor, float32, (batch, sequence length) representing the mutated sequence
            features (torch.Tensor): tensor, float32, (batch, num_features) representing the features

        Returns:
            torch.Tensor: tensor, float32, (batch, 1) representing the predicted value
        """
        # print('X_nucl shape:', X_nucl.shape)
        # print('X_mut_nucl shape:', X_mut_nucl.shape)
        # print('features shape:', features.shape)
        
        # convert the sequence to embeddings
        # (batch, sequence length, embed_dim)
        # convert the data to half precision
        transformer_out = self.transformer(X_nucl, X_mut_nucl, X_pbs, X_rtt, X_rtt_mut)
                
        # flatten the output of the transformer using a linear layer
        transformer_out = self.linear_transformer(transformer_out)
        
        # remove the last dimension of the transformer output
        transformer_out = transformer_out.squeeze(2)
        # transpose the output of the transformer for the GRU
        # transformer_out = transformer_out.transpose(1, 2)
        # transformer_out, _ = self.gru(transformer_out)
        
        # print('transformer_out shape:', transformer_out.shape)
        
        # convert the features to embeddings
        # (batch, sequence length, embed_dim)
        features_embed = self.d(features)
                
        # concatenate the output of the transformer and the features
        # (batch, sequence length, embed_dim)
        output = torch.cat([transformer_out, features_embed], dim=1)
        
        # print('output shape:', output.shape)
        
        # pass the output to the MLP decoder
        output = self.head(output)
        
        # print('output shape:', output.shape)
        # print('output:', output)
        
        return output
    
    
class AcceleratedNet(AccelerateMixin, skorch.NeuralNetRegressor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        
def attention(query, key, value, mask=None, dropout=None):
    "Compute 'Scaled Dot Product Attention'"
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(d_k)
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    p_attn = scores.softmax(dim=-1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn


def preprocess_transformer(X_train: pd.DataFrame, slice: bool=False) -> Dict[str, torch.Tensor]:
    """transform the transformer data into a format that can be used by the model

    Args:
        X_train (pd.DataFrame): the sequence and feature level data

    Returns:
        Dict[str, torch.Tensor]: dictionary of input names and their corresponding tensors, so that skorch can use them with the forward function
    """
    # sequence data
    wt_seq = X_train['wt-sequence'].values
    mut_seq = X_train['mut-sequence'].values
    # the rest are the features
    features = X_train.iloc[:, 2:26].values

    nut_to_ix = {'N': 4, 'A': 0, 'C': 1, 'G': 2, 'T': 3}
    X_nucl = torch.tensor([[nut_to_ix[n] for n in seq] for seq in wt_seq])
    X_mut_nucl = torch.tensor([[nut_to_ix[n] for n in seq] for seq in mut_seq])
    # create a linear embedding for pbs, protospacer, and rtt values
    X_pbs = torch.zeros(X_nucl.size(0), X_nucl.size(1))
    # X_protospacer = torch.zeros(X_nucl.size(0), X_nucl.size(1))
    X_rtt = torch.zeros(X_nucl.size(0), X_nucl.size(1))
    X_rtt_mut = torch.zeros(X_nucl.size(0), X_nucl.size(1))
    
    for i, (pbs_l, protospacer_l, rtt_l, rtt_mut_l, pbs_r, protospacer_r, rtt_r, rtt_mut_r) in enumerate(zip(X_train['pbs-location-l'].values, X_train['protospacer-location-l'].values, X_train['rtt-location-wt-l'].values, X_train['rtt-location-mut-l'].values, X_train['pbs-location-r'].values, X_train['protospacer-location-r'].values, X_train['rtt-location-wt-r'].values, X_train['rtt-location-mut-r'].values)):
        pbs_l = max(0, pbs_l)
        pbs_r = max(0, pbs_r)
        protospacer_l = max(0, protospacer_l)
        protospacer_r = max(0, protospacer_r)
        rtt_l = max(0, rtt_l)
        rtt_r = max(0, rtt_r)
        X_pbs[i, pbs_l:pbs_r] = 1
        # X_protospacer[i, protospacer_l:protospacer_r] = 1
        X_rtt[i, rtt_l:rtt_r] = 1
        X_rtt_mut[i, rtt_mut_l:rtt_mut_r] = 1
    
    result = {
        'X_nucl': X_nucl,
        'X_mut_nucl': X_mut_nucl,
        'X_pbs': X_pbs,
        # 'X_protospacer': X_protospacer,
        'X_rtt': X_rtt,
        'X_rtt_mut': X_rtt_mut,
        'features': torch.tensor(features).float()#.half()
    }

    
    return result

def train_transformer(train_fname: str, lr: float, batch_size: int, epochs: int, patience: int, num_runs: int, num_features: int, dropout: float = 0.1, percentage: str = 1, annot: bool = True) -> skorch.NeuralNetRegressor:
    """train the transformer model

    Args:
        train_fname (str): the name of the csv file containing the training data
        lr (float): learning rate
        batch_size (int): batch size
        epochs (int): number of epochs
        patience (int): number of epochs to wait before early stopping
        num_runs (int): number of repeated runs on one fold
        num_features (int): number of features to use for the MLP
        adjustment (str, optional): adjustment to the target value. Defaults to 'None'.
        dropout (float, optional): percentage of input units to drop. Defaults to 0.1.
        percentage (str, optional): percentage of the training data to use. Defaults to 1, meaning all the data will be used.

    Returns:
        skorch.NeuralNetRegressor: _description_
    """
    # load a dp dataset
    dp_dataset = pd.read_csv(os.path.join('models', 'data', 'transformer', train_fname))
    
    # remove rows with nan values
    dp_dataset = dp_dataset.dropna()
    
    # if percentage is less than 1, then use a subset of the data
    if percentage < 1:
        dp_dataset = dp_dataset.sample(frac=percentage, random_state=42)
    
    # TODO read the top 2000 rows only during development
    # dp_dataset = dp_dataset.head(2000)
    
    # data origin
    data_origin = os.path.basename(train_fname).split('-')[1]
    
    fold = 5
        
    # device
    device = torch.device('cuda')
    
    for i in range(fold):
        print(f'Fold {i+1} of {fold}')
        
        train = dp_dataset[dp_dataset['fold']!=i]
        X_train = train
        print(X_train.columns)
        y_train = train.iloc[:, -2]

        X_train = preprocess_transformer(X_train)
        y_train = torch.tensor(y_train.values, dtype=torch.float32).unsqueeze(1)
        
        # check if X_train contains nan values
        if torch.isnan(X_train['X_nucl']).any():
            print('X_nucl contains nan values')
        if torch.isnan(X_train['X_mut_nucl']).any():
            print('X_mut_nucl contains nan values')
        if torch.isnan(X_train['features']).any():
            print('features contains nan values')
        
        print("Training Transformer model...")
        
        best_val_loss = np.inf
        
        embed_dim = 4 if not annot else 6
        num_heads = 3 if annot else 2
    
        for j in range(num_runs):
            print(f'Run {j+1} of {num_runs}')
            # model
            m = PrimeDesignTransformer(embed_dim=embed_dim, sequence_length=50, num_heads=num_heads,pdropout=dropout, nonlin_func=nn.ReLU(), num_encoder_units=1, num_features=num_features, onehot=True, annot=annot, flash=False)
            
            accelerator = Accelerator(mixed_precision='bf16')
            
            # skorch model
            model = AcceleratedNet(
                m,
                accelerator=accelerator,
                criterion=nn.MSELoss,
                optimizer=torch.optim.AdamW,
                # optimizer__eps=1e-4,
                # optimizer=torch.optim.SGD,
                optimizer__lr=lr,
                device=None,
                batch_size=batch_size,
                max_epochs=epochs,
                train_split= skorch.dataset.ValidSplit(cv=5),
                # early stopping
                callbacks=[
                    skorch.callbacks.EarlyStopping(patience=patience),
                    skorch.callbacks.Checkpoint(monitor='valid_loss_best', 
                                    f_params=os.path.join('models', 'trained-models', 'transformer', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-tmp.pt"), 
                                    f_optimizer=None, 
                                    f_history=None,
                                    f_criterion=None),
                    skorch.callbacks.LRScheduler(policy=torch.optim.lr_scheduler.CosineAnnealingWarmRestarts , monitor='valid_loss', T_0=10, T_mult=1, eta_min=1e-3),
                    # skorch.callbacks.ProgressBar(),
                    # PrintParameterGradients()
                ]
            )
            
            model.initialize()
            # torch.nn.utils.clip_grad_norm_(m.parameters(), max_norm=1.0)
            
            model.fit(X_train, y_train)
            
            if np.min(model.history[:, 'valid_loss']) < best_val_loss:
                print(f'Best validation loss: {np.min(model.history[:, "valid_loss"])}')
                best_val_loss = np.min(model.history[:, 'valid_loss'])
                # rename the model file to the best model
                os.rename(os.path.join('models', 'trained-models', 'transformer', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-tmp.pt"), os.path.join('models', 'trained-models', 'transformer', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}.pt"))
            else: # delete the last model
                print(f'Validation loss: {np.min(model.history[:, "valid_loss"])} is not better than {best_val_loss}')
                os.remove(os.path.join('models', 'trained-models', 'transformer', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-tmp.pt"))
            
        
    return model

def predict_transformer(test_fname: str, num_features: int, adjustment: str = None, device: str = 'cuda', dropout: float=0, percentage: float = 1.0, annot: bool = False) -> skorch.NeuralNetRegressor:
    # model name
    fname = os.path.basename(test_fname)
    model_name =  fname.split('.')[0]
    model_name = '-'.join(model_name.split('-')[1:])
    models = [os.path.join('models', 'trained-models', 'transformer', f'{model_name}-fold-{i}.pt') for i in range(1, 6)]
    # Load the data
    test_data_all = pd.read_csv(os.path.join('models', 'data', 'transformer', test_fname))    
    # if percentage is less than 1, then use a subset of the data
    if percentage < 1:
        test_data_all = test_data_all.sample(frac=percentage, random_state=42)
    # remove rows with nan values
    test_data_all = test_data_all.dropna()
    # transform to float
    test_data_all.iloc[:, 2:26] = test_data_all.iloc[:, 2:26].astype(float)
    
    embed_dim = 4 if not annot else 6
    num_heads = 3 if annot else 2

    m = PrimeDesignTransformer(embed_dim=embed_dim, sequence_length=50, num_heads=num_heads,pdropout=dropout, nonlin_func=nn.ReLU(), num_encoder_units=1, num_features=num_features, onehot=True, annot=annot, flash=False)
    
    accelerator = Accelerator(mixed_precision='bf16')
            
    # skorch model
    tr_model = AcceleratedNet(
        m,
        accelerator=accelerator,
        criterion=nn.MSELoss,
        optimizer=torch.optim.AdamW,
    )

    prediction = {}
    performance = []

    # Load the models
    for i, model in enumerate(models):
        if not os.path.isfile(os.path.join('models', 'trained-models', 'transformer', f"{'-'.join(os.path.basename(test_fname).split('.')[0].split('-')[1:])}-fold-{i+1}.pt")):
            continue
        
        test_data = test_data_all[test_data_all['fold']==i]
        X_test = test_data
        y_test = test_data.iloc[:, -2]
        X_test = preprocess_transformer(X_test)
        y_test = y_test.values
        y_test = y_test.reshape(-1, 1)
        y_test = torch.tensor(y_test, dtype=torch.float32)
        tr_model.initialize()
        if adjustment:
            tr_model.load_params(f_params=os.path.join('models', 'trained-models', 'transformer', f"{'-'.join(os.path.basename(test_fname).split('.')[0].split('-')[1:])}-fold-{i+1}.pt"))
        else:
            tr_model.load_params(f_params=os.path.join('models', 'trained-models', 'transformer', f"{'-'.join(os.path.basename(test_fname).split('.')[0].split('-')[1:])}-fold-{i+1}.pt"))
        
        y_pred = tr_model.predict(X_test)
        if adjustment == 'log':
            y_pred = np.expm1(y_pred)

        pearson = np.corrcoef(y_test.T, y_pred.T)[0, 1]
        spearman = scipy.stats.spearmanr(y_test, y_pred)[0]

        print(f'Fold {i + 1} Pearson: {pearson}, Spearman: {spearman}')

        prediction[i] = y_pred
        performance.append((pearson, spearman))
    
    return prediction, performance


def tune_transformer(tune_fname: str, num_features: int, adjustment: str = None, device: str = 'cuda', dropout: float=0, percentage: float = 1.0, num_runs: int = 5, patience: int = 10) -> None:
    """perform hyperparameter tuning for the transformer model

    Args:
        tune_fname (str): the name of the csv file containing the test data
        num_features (int): number of features to use for the MLP
        adjustment (str, optional): adjustment to the target value. Defaults to 'None'.
        device (str, optional): device used for tuning. Defaults to 'cuda'.
        dropout (float, optional): percentage of input units to drop. Defaults to 0.1.
        percentage (float, optional): percentage of the training data to use. Defaults to 1.0.
        num_runs (int, optional): number of repeated runs on one fold. Defaults to 5.
    """
    # using gridsearchcv for hyperparameter tuning
    from sklearn.model_selection import GridSearchCV
    
    params_arch = {
        # 'module__embed_dim': [4, 8, 16],
        # 'module__num_heads': [2, 4, 8],
        'module__num_heads': [4],
        # 'module__pdropout': [0.3, 0.5, 0.8],
        'module__pdropout': [0.3],
        # 'module__mlp_embed_factor': [1, 2, 4],
        'module__mlp_embed_factor': [1],
        # 'module__num_encoder_units': [0, 1],
        'module__num_encoder_units': [1],
        # 'batch_size': [1012, 2048, 4096],
        # 'lr': [0.1, 0.05, 0.01],
        'module__join_option': ['stack', 'mean', 'concat'],
        'module__onehot': [True, False]
    }
    # for a grid using the full parameter space
    # list of lists of parameters
    params_arch = list(ParameterGrid(params_arch))
    
    params_train = {
        'criterion': [nn.MSELoss, nn.L1Loss, nn.SmoothL1Loss],
        'optimizer__lr': [0.01, 0.05, 0.1, 0.005],
        'batch_size': [1012, 2048, 4096, 8192],
    }
    
    params_train = list(ParameterGrid(params_train))
    
            
    # load a dp dataset
    dp_dataset = pd.read_csv(os.path.join('models', 'data', 'transformer', tune_fname))
    
    # remove rows with nan values
    dp_dataset = dp_dataset.dropna()
    
    # if percentage is less than 1, then use a subset of the data
    if percentage < 1:
        dp_dataset = dp_dataset.sample(frac=percentage, random_state=42)
        
    train_data = dp_dataset[dp_dataset['fold']!=0]
    test_data = dp_dataset[dp_dataset['fold']==0]
    X_train = train_data.iloc[:, :num_features+2]
    y_train = train_data.iloc[:, -2]
    X_test = test_data.iloc[:, :num_features+2]
    y_test = test_data.iloc[:, -2]
    
    X_train = preprocess_transformer(X_train)
    y_train = torch.tensor(y_train.values, dtype=torch.float32).unsqueeze(1)
    X_test = preprocess_transformer(X_test)
    y_test = torch.tensor(y_test.values, dtype=torch.float32).unsqueeze(1)
        
    # use fold 0 for tuning
    for ind, par in enumerate(params_train):
        performances = os.path.join('models', 'data', 'performance', 'transformer-train-fine-tune.csv')
        # check if the parameter has already been tuned
        if os.path.isfile(performances):
            performances = pd.read_csv(performances)
            # convert the dataframe to a dictionary
            condition = pd.Series([True]*len(performances))
            for p in par:
                condition = condition & performances[p] == str(par[p])
            row = performances[condition]
            if len(row['performance'].isna().values) > 0 and not row['performance'].isna().values[0]:
                # print(f'Parameter: {par} has already been tuned')
                # print('-'*50, '\n')
                par['performance'] = row['performance'].values[0]
                # continue
    for ind, par in enumerate(params_train):
        performances = os.path.join('models', 'data', 'performance', 'transformer-train-fine-tune.csv')
        # check if the parameter has already been tuned
        if os.path.isfile(performances):
            if 'performance' in par:
                print(f'Parameter: {par} has already been tuned')
                print('-'*50, '\n')
                continue
        
        performances = []
        print(f'Parameter: {par}')
        for run in range(num_runs):    
            t = time.time()
            print(f'Run {run+1} of {num_runs}')        
            accelerator = Accelerator(mixed_precision='bf16')
            
            # skorch model
            model = AcceleratedNet(
                PrimeDesignTransformer,
                module__embed_dim=4,
                module__sequence_length=99,
                module__num_heads=2,
                module__pdropout=dropout,
                module__mlp_embed_factor=2,
                module__nonlin_func=nn.ReLU(),
                module__num_encoder_units=2,
                module__join_option='stack',
                module__num_features=num_features,
                module__onehot=True,
                accelerator=accelerator,
                criterion=nn.MSELoss,
                optimizer=torch.optim.AdamW,
                # optimizer__eps=1e-4,
                # optimizer=torch.optim.SGD,
                optimizer__lr=0.01,
                max_epochs=500,
                device=None,
                batch_size=2048,
                train_split= skorch.dataset.ValidSplit(cv=5),
                # early stopping
                callbacks=[
                    skorch.callbacks.EarlyStopping(patience=patience),
                    skorch.callbacks.LRScheduler(policy=torch.optim.lr_scheduler.CosineAnnealingWarmRestarts , monitor='valid_loss', T_0=10, T_mult=1, eta_min=1e-3),
                    skorch.callbacks.Checkpoint(monitor='valid_loss_best', f_params='tmp.pt', f_optimizer=None, f_history=None, f_criterion=None),
                    # skorch.callbacks.ProgressBar(),
                    # PrintParameterGradients()
                ]
            )
            model.set_params(**par)
                        
            model.fit(X_train, y_train)
            # model.save_params(f_params='tmp.pt')
            
            # print(f'Parameter: {par}, val loss: {model.history[-1, "valid_loss"]}')
            # evaluate the model
            model = AcceleratedNet(
                PrimeDesignTransformer,
                module__embed_dim=4,
                module__sequence_length=99,
                module__num_heads=2,
                module__pdropout=dropout,
                module__mlp_embed_factor=2,
                module__nonlin_func=nn.ReLU(),
                module__num_encoder_units=2,
                module__join_option='stack',
                module__num_features=num_features,
                accelerator=accelerator,
                criterion=nn.MSELoss,
            )
            
            model.set_params(**par)
            model.initialize()
            
            model.load_params(f_params='tmp.pt')
            
            y_pred = model.predict(X_test)
            pearson = np.corrcoef(y_test.T, y_pred.T)[0, 1]
                        
            performances.append(pearson)
            # delete the temporary model
            os.remove('tmp.pt')
            print(f'Run time: {time.time() - t}')    
        par['performance'] = performances
        
        print(f'Parameter: {par}, Performance: {performances}')
        print('-'*50, '\n')
        
        # save the results in a csv file
        results = pd.DataFrame(params_train)
        results.to_csv(os.path.join('models', 'data', 'performance', 'transformer-train-fine-tune.csv'), index=False)