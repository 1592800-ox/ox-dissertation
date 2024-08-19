'''
Training DeepPrime
'''
from os.path import join as pjoin, basename

from models.deepprime import DeepPrime, preprocess_deep_prime, train_deep_prime


# org dp dataset
fname = pjoin('dp-pd-hek293t-pe2.csv')
train_deep_prime(fname, hidden_size=128, num_features=24, num_layers=1, dropout=0.05, epochs=200, batch_size=1024, lr=0.01, patience=20, device='cuda', num_runs=3)