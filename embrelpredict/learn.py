import torch
import embrelpredict.model as biclassmodel
import embrelpredict.evalclass as evalclass
import numpy as np
import sys
import logging
import os
import os.path as osp
import json
import math
import itertools
import pandas as pd

logger = logging.getLogger(__name__)

# The learn module provides methods for learning binary classifiers
# as well as for providing IO on the training and testing of the models


def _epochs_from_trainset_size(trainset_size):
    """Returns a sensible number of epochs for a given trainset_size

    Args:
       trainset_size the size of the training set (i.e. examples in 1 epoch)

    Returns:
       integer number of suggested epochs to train for
    """
    log10 = math.log10(trainset_size/2)
    order = round(log10 - 1)
    inv_order = 5 - order
    factor = math.pow(2, inv_order)
    base = 3
    return round(factor * base)


def _build_model(name='nn3', indim=600):
    """Build a pytorch binary classifier for a given input dimension

    Args:
        name one of 'nn1', 'nn2', 'nn3', 'logreg', 'alwaysT', 'alwaysF'

    Returns:
      a pytorch binary classifier model
    """
    # TODO: move to embrelpredict.model?
    if name == 'logreg':
        my_model = biclassmodel.LogisticRegression(indim)
    elif name == 'nn1':
        nn1 = {"layer_dims": [indim], "dropouts": [0.5]}
        my_model = biclassmodel.NNBiClassifier(indim, nn1['layer_dims'],
                                               nn1['dropouts'])
    elif name == 'nn2':
        if indim == 600:
            nn2 = {"layer_dims": [750, 400], "dropouts": [0.5, 0.5]}
        elif indim == 300:
            nn2 = {"layer_dims": [400, 150], "dropouts": [0.5, 0.5]}
        else:
            raise Exception('Unexpected input dimension %d' % indim)
        my_model = biclassmodel.NNBiClassifier(indim, nn2['layer_dims'],
                                               nn2['dropouts'])
    elif name == 'nn3':
        if indim == 600:
            nn3 = {"layer_dims": [750, 500, 250],
                   "dropouts": [0.5, 0.5, 0.5]}
        elif indim == 300:
            nn3 = {"layer_dims": [400, 200, 100],
                   "dropouts": [0.5, 0.5, 0.5]}
        else:
            raise Exception('Unexpected input dimension %d' % indim)
        my_model = biclassmodel.NNBiClassifier(indim, nn3['layer_dims'],
                                               nn3['dropouts'])
    elif name == 'alwaysT':
        my_model = biclassmodel.DummyBiClassifier(indim, predef=[0.01, 0.99])
    elif name == 'alwaysF':
        my_model = biclassmodel.DummyBiClassifier(indim, predef=[0.99, 0.01])
    else:
        raise Exception('Unknown model name %d' % name)
    return my_model


def load_rels_meta(relpath):
    """Extracts metadata about a folder with relpair files based on the filenames

    Args:
      relpath the path to the folder containing the word relation data, files in
        this folder must adhere to the standard naming reltype_relname__excnt.txt

    Returns:
      dataframe with columns 'type', 'name', 'cnt' and 'file'
    """
    rels = []
    for f in [f for f in os.listdir(relpath) if osp.isfile(osp.join(relpath, f))]:
        prefix_end = f.find('_')
        rel_end = f.find('__')
        ext_start = f.find('.txt')
        rel_type = f[0:prefix_end]
        rel_name = f[prefix_end + 1:rel_end]
        rel_ex_cnt = int(f[rel_end + 2:ext_start])
        rel = {"type": rel_type, "name": rel_name, "cnt": rel_ex_cnt, "file": f}
        rels.append(rel)
        # print(rel)
    rel_df = pd.DataFrame(rels)
    return rel_df


def learn_rel(relpath, rel_meta, data_loaders,
              single_rel_types=[],
              epochs_from_trainset_size_fn=_epochs_from_trainset_size,
              rel_filter=None, models=['logreg', 'nn2', 'nn3'], n_runs=5,
              train_input_disturber=None,
              debug_test_df=False,
              cuda=False):
    """ Train binary classifier models to learn a relation given a dataset

    Args:
       relpath path to the relation tsv files
       rel_meta dict or object with metadata about the relation to learn
       data_loaders dictionary of data_loader objects responsible for loading
                    and splitting the dataset
       single_rel_types list of rel type names which are not pairs, but
                    single words
       epochs_from_trainset_size_fn function from trainset size to number
                    of epochs
       rel_filter filter for the rel_meta to skip unwanted relations
       models list of model names to train
       n_runs times to train each model (to get average and stdv)
       train_input_disturber function to disturb an input batch

    Returns:
      An object with data summarising the learning result. It includes the
      rel_name, rel_type, number of epochs trained, number of positive samples
      for the relation and metrics for various models: base, best, and the
      specified models. Metrics include (average and stdv for) accuracy, f1,
      precision and recall.
    """
    cnt = rel_meta['cnt']
    rel_name = rel_meta['name']
    rel_type = rel_meta['type']
    empty_result = {"rel_name": rel_name, "rel_type": rel_type,
                    "pos_exs": cnt,
                    "emb_model_results": {}}
    if cnt < 75:
        print(rel_name, rel_type, 'too few examples')
        return empty_result

    if rel_filter and not rel_filter(rel_meta):
        print(rel_name, rel_type, 'not in rel_name filter')
        return empty_result

    emb_model_results = {}  # dict from 'emb name' to a list of model_results
    print('Training each model %d times...' % n_runs)
    for model, loader_name, run in itertools.product(
            models, data_loaders, range(n_runs)):
        print("run %d on model %s with vectors %s" %
              (run, model, loader_name))
        data_loader = data_loaders[loader_name]
        fpath = osp.join(relpath, rel_meta['file'])
        # load embeddings and labels
        if rel_type == 'rnd2rnd':
            X, Y, ds_n, ds_tc, ds_tf = data_loader.generate_random_pair_data(
                target_size=cnt*2)
        elif rel_type in single_rel_types:
            X, Y, ds_n, ds_tc, ds_tf = data_loader.load_single_data(fpath)
        else:
            X, Y, ds_n, ds_tc, ds_tf = data_loader.load_pair_data(fpath)

        indim = X.shape[1]

        msg = 'Expecting binary classifier but found max %d min %d' % (
            torch.max(Y), torch.min(Y))
        assert torch.max(Y) == 1 and torch.min(Y) == 0, msg

        print("\n\n\n", rel_meta['file'])
        epochs = epochs_from_trainset_size_fn(X.shape[0])  # from full dataset
        trainloader, validloader, testloader = data_loader.split_data(
            X, Y, seed=41)
        my_model = _build_model(model, indim)

        try:
            trainer = evalclass.ModelTrainer(my_model, cuda=cuda)
            pretrain_test_result = trainer.test(testloader)
            trainer.train(trainloader, validloader, epochs_list=range(epochs),
                          input_disturber=train_input_disturber)
            print('Finished %d epochs of training' % epochs)
            test_df = trainer.test_df(testloader, debug=debug_test_df)

            test_random_result = trainer.test_random(testloader)
            model_result = {"model": model, "i": run, "emb": loader_name,
                            "epochs": epochs, "pos_exs": cnt,
                            "dataset_size": ds_n,
                            "dataset_tok_cnt": ds_tc,
                            "dataset_tok_found": ds_tf,
                            # "trainer": trainer,
                            "trainer_df": trainer.df,  # to plot learning curve
                            "pretrain_test_result": pretrain_test_result,
                            "test_df": test_df,
                            "test_random_result": test_random_result}
            model_results = emb_model_results.get(loader_name, [])
            model_results.append(model_result)
            emb_model_results[loader_name] = model_results
        except:
            print("Unexpected error executing %s:" % model, sys.exc_info()[0])
            raise

        # del trainer # cannot delete the trainer as we need it later on
        del my_model
        del trainloader
        del validloader
        del testloader

    result = {"rel_name": rel_name, "rel_type": rel_type,
              "pos_exs": cnt,
              "emb_model_results": emb_model_results}
    return result


def store_learn_result(dir_path, learn_result):
    """Stores a learn_result in the specified dir_path

    This is done by convention in subfolders
    'reltype'/'rel_name'/'emb'/run_i/

    Each subfolder will contain several files. See _store_embrun_result
    """
    rel_path = osp.join(dir_path,
                        learn_result['rel_type'],
                        learn_result['rel_name'])
    for emb in learn_result['emb_model_results']:
        emb_path = osp.join(rel_path, emb)
        for i, emb_result in enumerate(learn_result['emb_model_results'][emb]):
            _store_embrun_result(emb_path, emb_result)


def load_learn_results(dir_path):
    """Loads all learn_results from a specified dir_path

    Returns:
      a list of learn_result objects
    """
    result = []
    for rel_type in [rt for rt in os.listdir(dir_path)
                     if osp.isdir(osp.join(dir_path, rt))]:
        rel_path = osp.join(dir_path, rel_type)
        for rel_name in [rn for rn in os.listdir(rel_path)
                         if osp.isdir(osp.join(rel_path, rn))]:
            result.append(load_learn_result(dir_path, rel_type, rel_name))
    return result


def load_learn_result(dir_path, rel_type, rel_name):
    """Loads a learn_result from the specified dir_path
    """
    result = {'rel_type': rel_type,
              'rel_name': rel_name}
    rel_dir = osp.join(dir_path, rel_type, rel_name)
    embs = [emb for emb in os.listdir(rel_dir)
            if osp.isdir(osp.join(rel_dir, emb))]
    emb_model_results = {}
    pos_exs = None
    for emb in embs:
        emb_dir = osp.join(rel_dir, emb)
        emb_model_res = []
        for model in [model for model in os.listdir(emb_dir)
                      if osp.isdir(osp.join(emb_dir, model))]:
            embmodel_dir = osp.join(emb_dir, model)
            runs = [run for run in os.listdir(embmodel_dir)
                    if osp.isdir(osp.join(embmodel_dir, run))]
            for run in runs:
                embrun_res = _load_embrun_result(osp.join(embmodel_dir, run))
                emb_model_res.append(embrun_res)
                if not pos_exs:
                    pos_exs = embrun_res['pos_exs']
        emb_model_results[emb] = emb_model_res
    result['emb_model_results'] = emb_model_results
    result['pos_exs'] = pos_exs
    return result


def _store_embrun_result(emb_dir, emb_result):
    """Stores an embedding run result in a folder under emb_dir

    The folder will be emb_dir/model_name/run_i

    The following files will be generated:
     - meta.json
     - train.tsv
     - test.tsv
     - pretrain-test.json
     - randomvec-test.json
    """
    odir = osp.join(emb_dir, emb_result['model'], 'run_%02d' % emb_result['i'])
    if not osp.exists(odir):
        os.makedirs(odir)

    meta = {key: emb_result[key] for key in ['model', 'i', 'emb', 'epochs',
                                             'pos_exs', 'dataset_size',
                                             'dataset_tok_cnt', 'dataset_tok_found']}
    meta['pos_exs'] = int(meta['pos_exs'])
    with open(osp.join(odir, 'meta.json'), 'w') as fp:
        json.dump(meta, fp)

    emb_result['trainer_df'].to_csv(osp.join(odir, 'train.tsv'), sep='\t', index=False)
    emb_result['test_df'].to_csv(osp.join(odir, 'test.tsv'), sep='\t', index=False)

    with open(osp.join(odir, 'pretrain-test.json'), 'w') as fp:
        json.dump(emb_result['pretrain_test_result'], fp)

    with open(osp.join(odir, 'randomvec-test.json'), 'w') as fp:
        json.dump(emb_result['test_random_result'], fp)


def _load_embrun_result(emb_dir):
    """Reads files in a emb_dir to re-create an embedding run learn result
    """
    with open(osp.join(emb_dir, 'meta.json')) as fp:
        result = json.load(fp)
    result['trainer_df'] = pd.read_csv(osp.join(emb_dir, 'train.tsv'), sep='\t')
    result['test_df'] = pd.read_csv(osp.join(emb_dir, 'test.tsv'), sep='\t')
    with open(osp.join(emb_dir, 'pretrain-test.json')) as fp:
        result['pretrain_test_result'] = json.load(fp)
    with open(osp.join(emb_dir, 'randomvec-test.json')) as fp:
        result['test_random_result'] = json.load(fp)
    return result
