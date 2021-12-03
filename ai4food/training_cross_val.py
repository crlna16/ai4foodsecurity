#!/usr/bin/env python

try:
    import nni
except ImportError:
    pass

import argparse
import os
import sys
import h5py
from evaluation_utils import metrics, train_epoch, validation_epoch, save_predictions, save_reference, save_predictions_majority

sys.path.append('../notebooks/starter_files/')
from utils.data_transform import PlanetTransform
from baseline_models import SpatiotemporalModel

path_to_pseltae = "models"
sys.path.append(path_to_pseltae)
from models.stclassifier import PseLTae, PseTae
from models.stclassifier_combined import PseLTaeCombined 

import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, random_split
from torch.optim import Adam
from torch.nn import CrossEntropyLoss
from sklearn.model_selection import KFold, StratifiedKFold
from datasets import EarthObservationDataset, PlanetDataset, Sentinel2Dataset, Sentinel1Dataset, CombinedDataset

import numpy as np
import geopandas as gpd
import pandas as pd
import copy
import time

def reset_weights(m):
    '''
    Try resetting model weights to avoid
    weight leakage.
    '''
    for layer in m.children():
        if hasattr(layer, 'reset_parameters'):
            print(f'Reset trainable parameters of layer = {layer}')
            layer.reset_parameters()

def main(args):
    
    # setting seeds for reproducability and method comparison
    np.random.seed(1)
    torch.manual_seed(1)
    torch.cuda.manual_seed_all(1)

    # construct the dataset
    if len(args.input_data)==1:
        test_dataset = PlanetDataset(args) 
    else:
        test_dataset = CombinedDataset(args) 
    
    label_ids = [1, 2, 3, 4, 5]
    label_names = ['Wheat', 'Barley', 'Canola', 'Lucerne/Medics', 'Small grain grazing']
    print(f'label_ids: {label_ids}')
    print(f'label_names: {label_names}\n')

    # set device to GPU, if available
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f'\nDevice {device}')

    if args.split=='train':
        
        print('Labels in train and valid / (test)')
        if len(args.input_data)==1:
            (unique, counts) = np.unique(test_dataset[:][1], return_counts=True)
        else:
            (unique, counts) = np.unique(test_dataset[:][0][1], return_counts=True)
        frequencies = np.asarray((unique, counts)).T
        print(frequencies)
        
        (unique, counts) = np.unique(test_dataset[:][1], return_counts=True)
        no_of_classes = unique.shape[0]
        weights_for_samples = 1/frequencies[:,1]
        weights_for_samples = weights_for_samples/np.sum(weights_for_samples)*no_of_classes 
        weights_for_samples = torch.Tensor(weights_for_samples).to(device) 
        criterion = CrossEntropyLoss(weight=weights_for_samples, reduction="mean") 
        #criterion = nn.NLLLoss(reduction='sum')
        
        kfold = KFold(n_splits=args.k_folds, shuffle=True, random_state=7) #StratifiedKFold(n_splits=args.k_folds, shuffle=True) 

        for fold, (train_ids, val_ids) in enumerate(kfold.split(test_dataset)):

            print('----------------------------------------------')
            print(f'STARTING FOLD {fold}')
            print('----------------------------------------------')

            # training
            best_loss = np.inf
            best_metric = np.inf
            best_epoch = 0
            patience_count = 0
            all_train_losses = []
            all_valid_losses = []
            log_scores= []

            train_subsampler = torch.utils.data.SubsetRandomSampler(train_ids)
            val_subsampler = torch.utils.data.SubsetRandomSampler(val_ids)

            train_loader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=8, drop_last=True, sampler=train_subsampler)
            valid_loader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=8, drop_last=True, sampler=val_subsampler)

            print('Size of train loader: ', len(train_loader), 'and val loader: ', len(valid_loader))
            (unique, counts) = np.unique(test_dataset[train_ids][:][1], return_counts=True)
            frequencies = np.asarray((unique, counts)).T
            print('Labels in train: ',frequencies)

            (unique, counts) = np.unique(test_dataset[val_ids][:][1], return_counts=True)
            frequencies = np.asarray((unique, counts)).T
            print('Labels in validation: ',frequencies)

            # instatiate the model
            if len(args.input_data)==1:
                if args.use_pselatae:
                    model = PseLTae(**model_config)  #PseTae(**model_config) # 
                else:
                    model = SpatiotemporalModel(input_dim=args.input_dim, num_classes=len(label_ids), sequencelength=args.sequence_length, spatial_backbone=args.spatial_backbone, temporal_backbone=args.temporal_backbone, device=device)
            else: model = PseLTaeCombined(**model_config)

            if torch.cuda.is_available():
                model = model.cuda()   
            # Initialize model optimizer and loss criterion:
            optimizer = Adam(model.parameters(), lr=1e-3, weight_decay=1e-6)

            for epoch in range(args.max_epochs):
                # train
                model.train()
                start_time = time.time()
                print(f'\nEpoch: {epoch}')
                classes = len(label_ids)
                train_loss, train_metric = train_epoch(model, optimizer, train_loader, classes, criterion, args, device=device)
                train_loss = train_loss.cpu().detach().numpy()[0]
                train_metric = train_metric.cpu().detach().numpy()[0]
                all_train_losses.append(train_loss)

                print(f'Training took {(time.time() - start_time) / 60:.2f} minutes, \
                        train_loss: {train_loss:.4}, eval_metric: {train_metric:.4}')
                start_time = time.time()

                # validation
                valid_loss, y_true, y_pred, *_, valid_metric = validation_epoch(model, 
                                                                                valid_loader, 
                                                                                classes, 
                                                                                criterion,
                                                                                args, 
                                                                                device=device)
                valid_loss = valid_loss.cpu().detach().numpy()[0]
                valid_metric = valid_metric.cpu().detach().numpy()[0]
                all_valid_losses.append(valid_loss)

                # calculate metrics
                scores = metrics(y_true.cpu(), y_pred.cpu())
                #scores_msg = ", ".join([f"{k}={v:.2f}" for (k, v) in scores.items()])
                scores["epoch"] = epoch
                scores["train_loss"] = train_loss
                scores["valid_loss"] = valid_loss
                log_scores.append(scores)
                print(f'Validation scores:')
                for key, value in scores.items():
                    print(f'{key:20s}: {value}')
                print(f'Validation took {(time.time() - start_time) / 60:.2f} minutes, \
                        valid_loss: {valid_loss:.4f}, eval_metric {valid_metric:.4}')
                # nni
                if args.nni:
                    nni.report_intermediate_result(valid_metric)

                # early stopping
                if valid_loss < best_loss:
                    best_loss = valid_loss
                    best_metric = valid_metric
                    best_epoch = epoch
                    best_model = copy.deepcopy(model)
                    best_optimizer = copy.deepcopy(optimizer)
                    best_preds = y_pred
                    patience_count = 0
                else:
                    patience_count += 1

                if patience_count == args.patience:
                    print(f'no improvement for {args.patience} epochs -> early stopping')
                    print(f'best metric: {best_metric:.2f} and best loss: {best_loss:.2f} at epoch: {best_epoch}')
                    break

                # save checkpoints
                if epoch % args.checkpoint_epoch == 0 and epoch != 0:
                    save_model_path = os.path.join(args.target_dir, f'epoch_{epoch}_model.pt')
                    torch.save(dict(model_state=model.state_dict(), optimizer_state=optimizer.state_dict(), epoch=epoch, log=log_scores), save_model_path)

            # nni
            if args.nni:
                nni.report_final_result(best_metric)

            # save best model
            save_model_path = os.path.join(args.target_dir, f'best_model_fold_{fold}.pt') 
            torch.save(dict(model_state=best_model.state_dict(), optimizer_state=best_optimizer.state_dict(), epoch=best_epoch, log=log_scores), save_model_path)
            print(f'saved best model to {save_model_path}')

            # save training and validation history
            with open(os.path.join(args.target_dir, f'train_losses_fold_{fold}.txt'), 'w') as f:
                for tl in all_train_losses:
                    f.write(f'{tl:.4f}\n')
            with open(os.path.join(args.target_dir, f'valid_losses_fold_{fold}.txt'), 'w') as f:
                for vl in all_valid_losses:
                    f.write(f'{vl:.4f}\n')

                print(f"\nINFO: Saved training and validation history ") 
                print(f"\nINFO: Epoch {epoch}: train_loss {train_loss:.2f}, valid_loss {valid_loss:.2f} ") 
            model.apply(reset_weights)
    else:
        # instatiate the model
        if len(args.input_data)==1:
            if args.use_pselatae:
                model = PseLTae(**model_config)  #PseTae(**model_config) # 
            else:
                model = SpatiotemporalModel(input_dim=args.input_dim, num_classes=len(label_ids), sequencelength=args.sequence_length, spatial_backbone=args.spatial_backbone, temporal_backbone=args.temporal_backbone, device=device)
        else: model = PseLTaeCombined(**model_config)

        if torch.cuda.is_available():
            model = model.cuda()   
                    
        test_loader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=args.num_workers)
        
    # make predictions   
    if args.save_preds:
        if args.split == 'train':
            test_loader = DataLoader(valid_dataset, batch_size=args.batch_size, num_workers=args.num_workers)
            save_predictions(save_model_path, model, test_loader, device, label_ids, label_names, args)

        else:
            test_loader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=args.num_workers)
            save_model_path = os.path.join(args.target_dir, 'best_model.pt')
            print(f'\nINFO: saving predictions from the {args.split} set')
            save_predictions_majority(args.target_dir, model, test_loader, device, label_ids, label_names, args, len(test_dataset))

    # save reference
    if args.save_ref:
        if args.split == 'train':
            test_loader = DataLoader(valid_dataset, batch_size=args.batch_size, num_workers=args.num_workers)
        else:
            test_loader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=args.num_workers)
        print(f'\nINFO: saving reference from the {args.split} set')
        save_reference(test_loader, device, label_ids, label_names, args)


def add_nni_params(args):
    args_nni = nni.get_next_parameter()
    assert all([key in args for key in args_nni.keys()]), 'need only valid parameters'
    args_dict = vars(args)
    # cast params that should be int to int if needed (nni may offer them as float)
    args_nni_casted = {key:(int(value) if type(args_dict[key]) is int else value) 
                        for key, value in args_nni.items()}
    args_dict.update(args_nni_casted)
    # adjust paths of model and prediction outputs so they get saved together with the other outputs
    nni_output_dir = os.path.expandvars('$NNI_OUTPUT_DIR')
    nni_path = os.path.join(nni_output_dir, os.path.basename(args_dict['target_dir']))
    args_dict['target_dir'] = nni_path
    return args

def get_paselatae_model_config(args, verbose=False):
    # adding PseLTae model configs
    include_extras = args.include_extras
    if include_extras: extra_size = 2
    else: extra_size = 0
    mlp2_first_layer = 128 + extra_size
    
    if len(args.input_data)==1:
        if args.input_data[0]=='planet':
            lms = 244
        elif args.input_data[0]=='planet-5':
            lms = 48
        elif args.input_data[0]=='sentinel-1':
            lms = 41
        config = {
                'mlp1': [args.input_dim[0],32,64],    # Number of neurons in the layers of MLP1
                'pooling': 'mean_std',   # Pixel-embeddings pooling strategy
                'mlp2': [mlp2_first_layer,mlp2_first_layer],     # Number of neurons in the layers of MLP2
                'n_head': args.n_head,   # Number of attention heads
                'd_k': args.d_k,                # Dimension of the key and query vectors
                'mlp3': [256,128],     # Number of neurons in the layers of MLP3
                'dropout': args.dropout,          # Dropout probability
                'T':1000,                # Maximum period for the positional encoding
                'lms':lms,                # Maximum sequence length for positional encoding (only necessary if positions == order) !!! change to 48 for planet-5
                'positions': 'bespoke',     # Positions to use for the positional encoding (bespoke / order)
                'mlp4': [128, 64, 32, 5], # Number of neurons in the layers of MLP4
                'd_model': 256,              # size of the embeddings (E), if input vectors are of a different size, a linear layer is used to project them to a d_model-dimensional space
                'geomfeat': include_extras,   # If 1 the precomputed geometrical features (f) are used in the PSE
                }

        model_config = dict(input_dim=args.input_dim[0], mlp1=config['mlp1'], pooling=config['pooling'],
                            mlp2=config['mlp2'], n_head=config['n_head'], d_k=config['d_k'], mlp3=config['mlp3'],
                            dropout=config['dropout'], T=config['T'], len_max_seq=config['lms'],
                            positions=None, #dt.date_positions if config['positions'] == 'bespoke' else None,
                            mlp4=config['mlp4'], d_model=config['d_model'])
    else:
    
        config = {
                'mlp1-planet': [args.input_dim[0],32,64],    # Number of neurons in the layers of MLP1
                'mlp1-s1': [args.input_dim[1],32,64],    # Number of neurons in the layers of MLP1
                'pooling': 'mean_std',   # Pixel-embeddings pooling strategy
                'mlp2': [mlp2_first_layer,mlp2_first_layer],     # Number of neurons in the layers of MLP2
                'n_head': args.n_head,   # Number of attention heads
                'd_k': args.d_k,                # Dimension of the key and query vectors
                'mlp3_planet': [256, 128],     # Number of neurons in the layers of MLP3
                'mlp3_s1': [256, 64],     # Number of neurons in the layers of MLP3
                'dropout': args.dropout,          # Dropout probability
                'T':1000,                # Maximum period for the positional encoding
                'lms_planet':244,                # Maximum sequence length for positional encoding (only necessary if positions == order) !!! change to 48 for planet-5
                'lms_s1': 41,
                'positions': 'bespoke',     # Positions to use for the positional encoding (bespoke / order)
                'mlp4': [128+64, 64, 32, 5], # Number of neurons in the layers of MLP4
                'd_model': 256,              # size of the embeddings (E), if input vectors are of a different size, a linear layer is used to project them to a d_model-dimensional space
                'geomfeat': include_extras,   # If 1 the precomputed geometrical features (f) are used in the PSE
                }

        model_config = dict(input_dim_planet=args.input_dim[0], input_dim_s1=args.input_dim[1], mlp1_planet=config['mlp1-planet'], mlp1_s1=config['mlp1-s1'], pooling=config['pooling'],
                            mlp2=config['mlp2'], n_head=config['n_head'], d_k=config['d_k'], mlp3_planet=config['mlp3_planet'], mlp3_s1=config['mlp3_s1'],
                            dropout=config['dropout'], T=config['T'], len_max_seq_planet=config['lms_planet'], len_max_seq_s1=config['lms_s1'],
                            positions=None, #dt.date_positions if config['positions'] == 'bespoke' else None,
                            mlp4=config['mlp4'], d_model=config['d_model'])
    if config['geomfeat']:
        model_config.update(with_extra=True, extra_size=extra_size) # extra_size number of extra features
    else:
        model_config.update(with_extra=False, extra_size=None)   
    
    if verbose: print('Model configs: ', model_config)
    
    return model_config


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev-data-dir', type=str, 
                        default='../../../shared_data/2021-ai4food/dev_data/south-africa')
    parser.add_argument('--input-data-type', type=str, default='extracted')
    parser.add_argument('--input-data', nargs="*", type=str, default='planet')
    parser.add_argument('--target-dir', type=str, default='.')
    parser.add_argument('--split', type=str, default='test', choices=['train', 'test']) 
    parser.add_argument('--k-folds', type=int, default=5)
    parser.add_argument('--nni', action='store_true', default=False)
    parser.add_argument('--save-preds', action='store_true', default=False) 
    parser.add_argument('--save-ref', action='store_true', default=False) 
    parser.add_argument('--max-epochs', type=int, default=100)
    parser.add_argument('--patience', type=int, default=10)
    parser.add_argument('--checkpoint-epoch', type=int, default=20)
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--input-dim', type=int, nargs="*", default=4)
    parser.add_argument('--sequence-length', type=int, default=74)
    parser.add_argument('--num-workers', type=int, default=8)
    parser.add_argument('--ndvi', type=int, default=0, choices=[0, 1])
    parser.add_argument('--nri', type=int, default=0, choices=[0, 1])
    parser.add_argument('--use-pselatae', type=int, default=1, choices=[0,1])
    parser.add_argument('--temporal-backbone', type=str, default='lstm', 
                        choices=["inceptiontime", "lstm", "msresnet", "starrnn", "tempcnn", "transformermodel"])
    parser.add_argument('--spatial-backbone', type=str, default='mobilenet_v3_small', 
                        choices=['resnet18', 'resnet34', 'resnet50', 'resnet101','resnext50_32x4d','resnext50_32x4d',
                                'wide_resnet50_2', 'mobilenet_v3_large',
                                "mobilenet_v3_small", 'vgg11', 'vgg11_bn', 'vgg13', 'vgg13_bn', 'vgg16',
                                'vgg16_bn', 'vgg19_bn', 'vgg19', "alexnet", 'squeezenet1_0'])
    parser.add_argument('--fill-value', type=bool, default=0)
    # for psalatae model
    parser.add_argument('--include-extras', type=int, default=0, choices=[0, 1])
    parser.add_argument('--n-head', type=int, default=16)
    parser.add_argument('--d-k', type=int, default=8)
    parser.add_argument('--dropout', type=float, default=0.2)
    args = parser.parse_args()

    if args.nni:
        args = add_nni_params(args)

    print('\nbegin args key / value')
    for key, value in vars(args).items():
        print(f'{key:20s}: {value}')
    print('end args keys / value\n')
    
    if args.use_pselatae:
        model_config = get_paselatae_model_config(args, verbose=True)
        args.model_config = model_config

    main(args)

