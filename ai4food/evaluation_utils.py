import os
import numpy as np
import pandas as pd
import sklearn.metrics
import torch
import torch.nn as nn
from tqdm import tqdm
import torch.multiprocessing
torch.multiprocessing.set_sharing_strategy('file_system')

def metrics(y_true, y_pred):
    """
    THIS FUNCTION DETERMINES THE EVALUATION METRICS OF THE MODEL

    :param y_true: ground-truth labels
    :param y_pred: predicted labels

    :return: dictionary of Accuracy, Kappa, F1, Recall, and Precision
    """
    accuracy = sklearn.metrics.accuracy_score(y_true, y_pred)
    kappa = sklearn.metrics.cohen_kappa_score(y_true, y_pred)
    f1_micro = sklearn.metrics.f1_score(y_true, y_pred, average="micro", zero_division=0)
    f1_macro = sklearn.metrics.f1_score(y_true, y_pred, average="macro", zero_division=0)
    f1_weighted = sklearn.metrics.f1_score(y_true, y_pred, average="weighted", zero_division=0)
    recall_micro = sklearn.metrics.recall_score(y_true, y_pred, average="micro")
    recall_macro = sklearn.metrics.recall_score(y_true, y_pred, average="macro")
    recall_weighted = sklearn.metrics.recall_score(y_true, y_pred, average="weighted")
    precision_micro = sklearn.metrics.precision_score(y_true, y_pred, average="micro", zero_division=0)
    precision_macro = sklearn.metrics.precision_score(y_true, y_pred, average="macro", zero_division=0)
    precision_weighted = sklearn.metrics.precision_score(y_true, y_pred, average="weighted")

    return dict(
        accuracy=accuracy,
        kappa=kappa,
        f1_micro=f1_micro,
        f1_macro=f1_macro,
        f1_weighted=f1_weighted,
        recall_micro=recall_micro,
        recall_macro=recall_macro,
        recall_weighted=recall_weighted,
        precision_micro=precision_micro,
        precision_macro=precision_macro,
        precision_weighted=precision_weighted,
    )

def bin_cross_entr_each_crop(logprobs, y_true, classes, device, args):
    '''
    calculates binary cross entropy for each class
    and sums the result up
    :param y_pred: model predictions
    :pram y_target: target
    :param classes: nr of classes
    
    :return: sum of binary cross entropy for each class 
    '''
    bin_ce = 0
    sm = nn.Softmax()
    loss_bc = nn.BCELoss()
    y_prob = sm(logprobs)
    # convert to one-hot representation
    y_true_onehot = torch.FloatTensor(args.batch_size, classes)
    y_true_onehot.zero_()
    y_true_onehot= y_true_onehot.to(device)
    y_true_onehot.scatter_(1,y_true.view(-1,1), 1)
    for i in range(classes):
        bin_ce+=loss_bc(y_prob[:,i], y_true_onehot[:,i].float())

    return bin_ce



def train_epoch(model, optimizer, dataloader, classes, args, device='cpu'):
    """
    THIS FUNCTION ITERATES A SINGLE EPOCH FOR TRAINING

    :param model: torch model for training
    :param optimizer: torch training optimizer
    :param criterion: torch objective for loss calculation
    :param dataloader: training data loader
    :param device: where to run the epoch

    :return: loss
    """
    model.train()
    losses = list()
    with tqdm(enumerate(dataloader), total=len(dataloader),position=0, leave=True) as iterator:
        for idx, batch in iterator:
            optimizer.zero_grad()
            x, y_true, _, _ = batch
            logprobs = model(x.to(device))
            y_true = y_true.to(device)

            loss = bin_cross_entr_each_crop(logprobs, y_true, classes, device, args)
            #loss = criterion(logprobs, y_true)
            loss.backward()
            optimizer.step()
            iterator.set_description(f"train loss={loss:.2f}")
            losses.append(loss)
    return torch.stack(losses)


def validation_epoch(model, dataloader, classes, args, device='cpu'):
    """
    THIS FUNCTION ITERATES A SINGLE EPOCH FOR VALIDATION

    :param model: torch model for validation
    :param criterion: torch objective for loss calculation
    :param dataloader: validation data loader
    :param device: where to run the epoch

    :return: loss, y_true, y_pred, y_score, field_id
    """
    model.eval()
    with torch.no_grad():
        losses = list()
        y_true_list = list()
        y_pred_list = list()
        y_score_list = list()
        field_ids_list = list()
        with tqdm(enumerate(dataloader), total=len(dataloader), position=0, leave=True) as iterator:
            for idx, batch in iterator:
                x, y_true, _, field_id = batch
                logprobs = model(x.to(device))
                y_true = y_true.to(device)
                
                loss = bin_cross_entr_each_crop(logprobs, y_true, classes, device, args)
                #loss = criterion(logprobs, y_true.to(device))
                iterator.set_description(f"valid loss={loss:.2f}")
                losses.append(loss)
                y_true_list.append(y_true)
                y_pred_list.append(logprobs.argmax(-1))
                y_score_list.append(logprobs.exp())
                field_ids_list.append(field_id)
        return torch.stack(losses), torch.cat(y_true_list), torch.cat(y_pred_list), torch.cat(y_score_list), torch.cat(field_ids_list)



def save_predictions(save_model_path, model, data_loader, device, label_ids, label_names, args):
    if os.path.exists(save_model_path):
        checkpoint = torch.load(save_model_path)
        START_EPOCH = checkpoint["epoch"]
        log = checkpoint["log"]
        model.load_state_dict(checkpoint["model_state"])
        model.eval()
        print(f"INFO: Resuming from {save_model_path}, epoch {START_EPOCH}")

        # list of dictionaries with predictions:
        output_list=[]
        softmax=torch.nn.Softmax(dim=1)

        with torch.no_grad():
            with tqdm(enumerate(data_loader), total=len(data_loader), position=0, leave=True) as iterator:
                for idx, batch in iterator:
                    X, _, _, fid = batch
                    logits = model(X.to(device))
                    predicted_probabilities = softmax(logits).cpu().detach().numpy()[0]
                    predicted_class = np.argmax(predicted_probabilities)
                    output_list.append({'fid': fid.cpu().detach().numpy()[0],
                                'crop_id': label_ids[predicted_class],
                                'crop_name': label_names[predicted_class],
                                'crop_probs': np.array(predicted_probabilities)})

        #  save predictions into output json:
        if args.split == 'train':
            output_name = os.path.join(args.target_dir, 'validation.json')
            print(f'Validation was saved to location: {(output_name)}')
        else:
            output_name = os.path.join(args.target_dir, 'submission.json')
            print(f'Submission was saved to location: {(output_name)}')
        output_frame = pd.DataFrame.from_dict(output_list)
        print(output_frame.head())
        output_frame.to_json(output_name)

    else:
        print('INFO: no best model found ...')
