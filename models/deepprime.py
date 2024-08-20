import os
import numpy as np
import pandas as pd
import sklearn.metrics
import skorch.helper
import torch
import torch.nn.functional as F
import torch.nn as nn
from glob import glob
from typing import Dict, Tuple
import sys
import scipy
import sklearn
import torch.utils
import skorch
from sklearn.preprocessing import StandardScaler

util_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
sys.path.append(util_path)

from utils.data_utils import onehot_encode_series
from utils.ml_utils import undersample


class DPEmbedding(nn.Module):
    '''
    Produce an embedding of the input nucleotide sequences
    '''
    def __init__(self):
        super(DPEmbedding, self).__init__()
        self.embedding = nn.Embedding(5, 4, padding_idx=0)
        
    def forward(self, g):
        return self.embedding(g)

class DeepPrime(nn.Module):
    '''
    requires hidden size and number of layers of the 
    GRU, number of features in the feature vector, and dropout rate
    '''
    def __init__(self, hidden_size, num_layers, num_features=24, dropout=0.1):
        super(DeepPrime, self).__init__()
        self.embedding = DPEmbedding()
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        self.c1 = nn.Sequential(
            nn.Conv2d(in_channels=4, out_channels=128, kernel_size=(2, 3), stride=1, padding=(0, 1)),
            nn.BatchNorm2d(128),
            nn.GELU(),
        )
        self.c2 = nn.Sequential(
            nn.Conv1d(in_channels=128, out_channels=108, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm1d(108),
            nn.GELU(),
            nn.AvgPool1d(kernel_size=2, stride=2),

            nn.Conv1d(in_channels=108, out_channels=108, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm1d(108),
            nn.GELU(),
            nn.AvgPool1d(kernel_size=2, stride=2),

            nn.Conv1d(in_channels=108, out_channels=128, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm1d(128),
            nn.GELU(),
            nn.AvgPool1d(kernel_size=2, stride=2),
        )

        self.r = nn.GRU(128, hidden_size, num_layers, batch_first=True, bidirectional=True)

        self.s = nn.Linear(2 * hidden_size, 12, bias=False)

        self.d = nn.Sequential(
            nn.Linear(num_features, 96, bias=False),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(96, 64, bias=False),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 128, bias=False)
        )

        self.head = nn.Sequential(
            nn.BatchNorm1d(140),
            nn.Dropout(dropout),
            nn.Linear(140, 1, bias=True),
        )

    # g is the stacked gene sequences(wildtype and edited) and x is the feature vector
    def forward(self, g, x):
        g = g.to('cuda').long()
        x = x.to('cuda')        
        g = self.embedding(g)
        
        # Ensure g is 4D
        if g.dim() == 3:
            g = g.unsqueeze(3)  # Add an extra dimension at the end
        # print("Shape of g after unsqueeze:", g.shape)

        # reshape to the format (batch_size, channels, height, width)
        g = g.permute(0, 2, 1, 3)
        
        # Pass the data through the Conv2d layers
        g = self.c1(g)

        # Remove the last dimension (width=1)
        g = torch.squeeze(g, 3)

        # Pass the data through the Conv1d layers
        g = self.c2(g)

        # Transpose for the GRU layer
        g, _ = self.r(torch.transpose(g, 1, 2))

        # Get the last hidden state
        g = self.s(g[:, -1, :])

        x = self.d(x)
        # print("Shape of x after dense layers d:", x.shape)

        out = self.head(torch.cat((g, x), dim=1))
        # print("Shape of out after head:", out.shape)

        return F.softplus(out)
    
# Custom loss function, adjusting for more frequent low efficiency values
class WeightedLoss(nn.Module):
    def __init__(self):
        super(WeightedLoss, self).__init__()
        self.base_loss = nn.MSELoss()  # or nn.CrossEntropyLoss() for classification

    def forward(self, outputs, targets):
        weights = self.calculate_weights(targets)
        loss = self.base_loss(outputs, targets)
        weighted_loss = loss * weights
        return weighted_loss.mean()

    @staticmethod
    def calculate_weights(efficiencies):
        weights = torch.exp(6 * (torch.log(efficiencies + 1) - 3) + 1)
        weights = torch.min(weights, torch.tensor(5.0))
        return weights

# returns a loaded data loader
def preprocess_deep_prime(X_train: pd.DataFrame, source: str = 'dp') -> Dict[str, torch.Tensor]:
    '''
    Preprocesses the data for the DeepPrime model
    '''
    # sequence data
    wt_seq = X_train['wt-sequence'].values
    mut_seq = X_train['mut-sequence'].values
    
    # crop the sequences to 74bp if longer
    print(len(wt_seq[0]))
    if len(wt_seq[0]) > 74:
        wt_seq = [seq[:74] for seq in wt_seq]
        mut_seq = [seq[:74] for seq in mut_seq]
    
    # the rest are the features
    features = X_train.drop(columns=['wt-sequence', 'mut-sequence']).values
    
    # concatenate the sequences
    seqs = []
    for wt, mut in zip(wt_seq, mut_seq):
        seqs.append(wt + mut)
    
    if source != 'org':
        nut_to_ix = {'N': 0, 'A': 1, 'C': 2, 'G': 3, 'T': 4}
    else:
        nut_to_ix = {'x': 0, 'A': 1, 'C': 2, 'G': 3, 'T': 4}

    output = {
        'g': torch.tensor([[nut_to_ix[n] for n in seq] for seq in seqs], dtype=torch.float32),
        'x': torch.tensor(features, dtype=torch.float32)
    }
    
    return output

def train_deep_prime(train_fname: str, hidden_size: int, num_layers: int, num_features: int, dropout: float, device: str, epochs: int, lr: float, batch_size: int, patience: int, num_runs: int = 3, source: str = 'dp') -> skorch.NeuralNet:
    '''
    Trains the DeepPrime model
    '''
    # load a dp dataset
    if source == 'org': # dp features
        dp_dataset = pd.read_csv(os.path.join('models', 'data', 'deepprime-org', train_fname))
    else:
        dp_dataset = pd.read_csv(os.path.join('models', 'data', 'deepprime', train_fname))
    
    # standardize the scalar values at column 2:26
    # scalar = StandardScaler()
    # dp_dataset.iloc[:, 2:26] = scalar.fit_transform(dp_dataset.iloc[:, 2:26])
    
    # data origin
    data_origin = os.path.basename(train_fname).split('-')[1]
    
    fold = 5
    
    print(dp_dataset.columns)

    for i in range(fold):
        print(f'Fold {i+1} of {fold}')
        train = dp_dataset[dp_dataset['fold']!=i]
        X_train = train.iloc[:, :num_features+2]
        y_train = train.iloc[:, -2]
        
        # if adjustment == 'log':
        #     y_train = np.log1p(y_train)
        # elif adjustment == 'undersample':
        #     X_train, y_train = undersample(X_train, y_train)
        

        X_train = preprocess_deep_prime(X_train, source)
        y_train = torch.tensor(y_train.values, dtype=torch.float32).unsqueeze(1)
        
        print("Training DeepPrime model...")
        
        best_val_loss = np.inf

        for j in range(num_runs):
            model = skorch.NeuralNetRegressor(
                DeepPrime(hidden_size, num_layers, num_features, dropout),
                criterion=nn.MSELoss,
                optimizer=torch.optim.AdamW,
                optimizer__lr=lr,
                device=device,
                batch_size=batch_size,
                max_epochs=epochs,
                train_split= skorch.dataset.ValidSplit(cv=5),
                # early stopping
                callbacks=[
                    skorch.callbacks.EarlyStopping(patience=patience),
                    skorch.callbacks.Checkpoint(monitor='valid_loss_best', 
                                    f_params=os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-tmp.pt"), 
                                    f_optimizer=os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-optimizer-tmp.pt"), 
                                    f_history=os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-history-tmp.json"),
                                    f_criterion=None),
                    skorch.callbacks.LRScheduler(policy=torch.optim.lr_scheduler.CosineAnnealingWarmRestarts , monitor='valid_loss', T_0=15, T_mult=1),
                    # skorch.callbacks.LRScheduler(policy=torch.optim.lr_scheduler.ReduceLROnPlateau, monitor='valid_loss', factor=0.5, patience=3, min_lr=1e-6),
                    # skorch.callbacks.ProgressBar()
                ]
            )
            print(f'Run {j+1} of {num_runs}')
            # Train the model
            model.fit(X_train, y_train)
            # check if validation loss is better
            valid_losses = model.history[:, 'valid_loss']
            # find the minimum validation loss
            min_valid_loss = min(valid_losses)
            if min_valid_loss < best_val_loss:
                print(f"Validation loss improved from {best_val_loss} to {min_valid_loss}")
                best_val_loss = min_valid_loss
                # rename the save model 
                os.rename(os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-tmp.pt"), os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}.pt"))
                os.rename(os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-optimizer-tmp.pt"), os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-optimizer.pt"))
                os.rename(os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-history-tmp.json"), os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-history.json"))
            else:
                print(f"Validation loss did not improve from {best_val_loss}")
                # remove the temporary files
                os.remove(os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-tmp.pt"))
                os.remove(os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-optimizer-tmp.pt"))
                os.remove(os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(train_fname).split('.')[0].split('-')[1:])}-fold-{i+1}-history-tmp.json"))
        print("Training done.")
        
        del model
        torch.cuda.empty_cache()

    # return model

def predict_deep_prime(test_fname: str, hidden_size: int = 128, num_layers: int = 1, num_features: int = 24, dropout: float = 0, adjustment: str = None, source: str='dp') -> Tuple[Dict[int, np.ndarray], np.ndarray]:
    """Make predictions using the DeepPrime model

    Args:
        test_fname (str): Base name of the test file
    Returns:
        Dict[str, np.ndarray]: The predictions result of the model from each fold
    """
    # model name
    fname = os.path.basename(test_fname)
    model_name =  fname.split('.')[0]
    model_name = '-'.join(model_name.split('-')[1:])
    models = [os.path.join('models', 'trained-models', 'deepprime', f'{model_name}-fold-{i}.pt') for i in range(1, 6)]
    # Load the data
    test_data_all = pd.read_csv(os.path.join('models', 'data', 'deepprime', test_fname))    
    # apply standard scalar
    # cast all numeric columns to float
    test_data_all.iloc[:, 2:26] = test_data_all.iloc[:, 2:26].astype(float)
    # scalar = StandardScaler()
    # test_data_all.iloc[:, 2:26] = scalar.fit_transform(test_data_all.iloc[:, 2:26])

    dp_model = skorch.NeuralNetRegressor(
        DeepPrime(hidden_size, num_layers, num_features, dropout),
        # criterion=nn.MSELoss,
        # optimizer=torch.optim.Adam,
        device='cuda' if torch.cuda.is_available() else 'cpu',
    )

    prediction = {}
    performance = []

    # Load the models
    for i, model in enumerate(models):
        test_data = test_data_all[test_data_all['fold']==i]
        X_test = test_data.iloc[:, :num_features+2]
        y_test = test_data.iloc[:, -2]
        X_test = preprocess_deep_prime(X_test, source)
        y_test = y_test.values
        y_test = y_test.reshape(-1, 1)
        y_test = torch.tensor(y_test, dtype=torch.float32)
        dp_model.initialize()
        if adjustment:
            dp_model.load_params(f_params=os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(test_fname).split('.')[0].split('-')[1:])}-fold-{i+1}.pt"))
        else:
            dp_model.load_params(f_params=os.path.join('models', 'trained-models', 'deepprime', f"{'-'.join(os.path.basename(test_fname).split('.')[0].split('-')[1:])}-fold-{i+1}.pt"))
        
        y_pred = dp_model.predict(X_test)
        if adjustment == 'log':
            y_pred = np.expm1(y_pred)

        pearson = np.corrcoef(y_test.T, y_pred.T)[0, 1]
        spearman = scipy.stats.spearmanr(y_test, y_pred)[0]

        print(f'Fold {i + 1} Pearson: {pearson}, Spearman: {spearman}')

        prediction[i] = y_pred
        performance.append((pearson, spearman))

    del dp_model    
    torch.cuda.empty_cache()
    
    return prediction, performance