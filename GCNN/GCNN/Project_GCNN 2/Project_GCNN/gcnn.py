# -*- coding: utf-8 -*-
"""GCNN.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1XRoCiYyQXiUyj0dXZh-TwGt1D8Z-Qu_R
"""

# !pip install --upgrade setuptools

# !pip install tensorflow=='2.5'

# Commented out IPython magic to ensure Python compatibility.
# %pip install dgl

# Commented out IPython magic to ensure Python compatibility.
# %pip install keras-gcn

# !pip install pandas
# !pip install keras

# Commented out IPython magic to ensure Python compatibility.
# %pip install absl

# Commented out IPython magic to ensure Python compatibility.
# %pip install tensorflow

import json
import logging
import os
import random
import time
from typing import Sequence

from absl import app
from absl import flags
import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from keras_gcn import GraphConv

def build_base_data_and_vocab():
  """Builds vocabulary from the training data files."""
  vocab = set()
  cognates = []
  filepath = 'data_dir/training-mod-0.10.tsv'
  print('Preparing base training data from %s ...', filepath)
  with open(filepath, 'r', encoding='utf-8') as f:
    # Skip header.
    next(f)
    for line in f:
      parts = tuple(line.strip('\n').split('\t')[1:])
      for p in parts:
        for c in p.split():
          vocab.add(c)
      cognates.append([p.strip() for p in parts])
  vocab = ['<PAD>', '<EOS>', '<BOS>', '<UNK>', '<TARGET>', '<BLANK>'] + sorted(
      list(vocab))
  # print("COGSETS:",cogsets,"**********************","\n")
  # print("VOCAB:",vocab,"$$$$$$$$$$$$$$$$$$$$$$$","\n")
  return cognates, vocab

def get_hparams():
  """Builds hyper-parameter dictionary from flags or file."""
  with open('checkpoint_dir/hparams.json', 'r') as f:
    hparams = json.load(f)
    print('HParams: %s', hparams)
    # print("HPARAMS",hparams,"\n")
    return hparams

def expand_training_set(cogsets):
  """Expands the dataset to all possible variations."""
  print('Expanding training data ...')
  nlangs = len(cogsets[0])
  all_samples = []
  for cs in cogsets:
    # Find all valid positions.
    isample = []
    for i in range(nlangs):
      if cs[i]:
        isample.append(cs[i])
      else:
        isample.append('<BLANK>')
    all_samples.append(isample)
  random.shuffle(all_samples)
  # print("ALL SAMPLES:",all_samples,"\n")
  return all_samples

def build_train_dataset(all_samples, batch_size, nlangs, max_length, char2idx):
  """Creates train dataset from the generator."""
  # Create data generators to feed into the networks.
  def la_gen():
    while True:
      for icset in all_samples:
        inputs = []
        targets = []
        input_mask = []
        target_mask = []
        # Get the present items.
        valids = [i for i in range(len(icset)) if icset[i] != '<BLANK>']
        # Select how many inputs will be present to provide information.
        num_present = random.randint(1, len(valids))
        present = random.sample(valids, num_present)
        # Create the actual data content.
        for i in range(len(icset)):
          # Create a max_length sequence.
          template = [char2idx['<BLANK>']] * max_length
          seq = [char2idx['<BOS>']] + [
              char2idx[c] if c in char2idx else char2idx['<UNK>']
              for c in icset[i].split()
          ] + [char2idx['<EOS>']]
          for j in range(min(len(seq), max_length)):
            template[j] = seq[j]
          targets.append(template)
          inputs.append(template)
          # If the sequence if valid, get gradient from it.
          if i in valids:
            target_mask.append([1.0] * max_length)
          else:
            target_mask.append([0.0] * max_length)
          # If the sequence should be present, don't mask it.
          if i in present:
            input_mask.append([1.0] * max_length)
          else:
            input_mask.append([0.0] * max_length)
        # Convert to required tensor formats.
        inputs = tf.constant([inputs], dtype='int32')
        targets = tf.constant([targets], dtype='int32')
        input_mask = tf.constant([input_mask], dtype='float32')
        target_mask = tf.constant([target_mask], dtype='float32')

        yield (inputs, targets, input_mask, target_mask)

  return tf.data.Dataset.from_generator(
      la_gen,
      output_signature=(tf.TensorSpec(
          shape=(batch_size, nlangs, max_length), dtype='int32'),
                        tf.TensorSpec(
                            shape=(batch_size, nlangs, max_length),
                            dtype='int32'),
                        tf.TensorSpec(
                            shape=(batch_size, nlangs, max_length),
                            dtype='float32'),
                        tf.TensorSpec(
                            shape=(batch_size, nlangs, max_length),
                            dtype='float32')))

# from keras_dgl.layers import GraphCNN
class Infiller(tf.keras.Model):
  """The infiller convolutional model."""

  def __init__(self, vocab_size, hparams, batch_size, nlangs, max_length):
    super(Infiller, self).__init__()
    self.batch_size = batch_size
    self.units = hparams['filters']
    self.kernel_width = hparams['kernel_width']
    self.vocab_size = vocab_size
    self.embedding_dim = hparams['embedding_dim']
    self.nlangs = nlangs
    self.max_length = max_length
    self.scale_pos = hparams['sfactor']

    ##-------- Embedding layers in Encoder ------- ##
    self.embedding = tf.keras.layers.Embedding(self.vocab_size,
                                               self.embedding_dim)

    ##-------- Convolution ------- ##
    self.conv = GraphConv(
        units=self.units, kernel_size=(nlangs, hparams['kernel_width']))

    ##-------- Nonlinearity ------- ##
    if hparams['nonlinearity'] == 'leaky_relu':
      self.act = tf.keras.layers.LeakyReLU()
    elif hparams['nonlinearity'] == 'relu':
      self.act = tf.keras.layers.ReLU()
    else:
      self.act = tf.keras.layers.Activation('tanh')

    ##-------- Dropout ------- ##
    self.dropout = tf.keras.layers.Dropout(hparams['dropout'])

    ##-------- Deconvolution ------- ##
    self.deconv = GraphConv(
        units=self.vocab_size, kernel_size=(nlangs, hparams['kernel_width']))

  def call(self, inputs, input_mask, training):
    # Reshape the mask.
    rmask = tf.repeat(input_mask, self.embedding_dim, axis=-1)
    rmask = tf.reshape(
        rmask,
        shape=(self.batch_size, self.nlangs, self.max_length,
               self.embedding_dim))

    # Embed the inputs.
    inputs = self.embedding(inputs)
    inputs = inputs * rmask

    # Scale the inputs.
    sfactor = (self.nlangs * self.max_length) / tf.math.reduce_sum(input_mask)
    if self.scale_pos == 'inputs':
      inputs = inputs * sfactor

    # Convolve
    inputs = self.conv(inputs)
    if self.scale_pos == 'conv':
      inputs = inputs * sfactor
    inputs = self.dropout(inputs, training=training)
    inputs = self.act(inputs)

    # Deconvolve
    logits = self.deconv(inputs)

    return logits

@tf.function
def loss_function(real, pred, mask):
  # real shape = (BATCH_SIZE, max_length_output)
  # pred shape = (BATCH_SIZE, max_length_output, tar_vocab_size )
  cross_entropy = tf.keras.losses.SparseCategoricalCrossentropy(
      from_logits=True, reduction='none')
  loss = cross_entropy(y_true=real, y_pred=pred)
  loss = mask * loss
  loss = tf.reduce_sum(loss)
  return loss

@tf.function
def train_step(infiller, optimizer, inp, inp_mask, targ, targ_mask):
  """Single training step."""
  loss = 0
  with tf.GradientTape() as tape:
    logits = infiller(inp, inp_mask, training=True)
    loss = loss_function(targ, logits, targ_mask)
  variables = infiller.trainable_variables
  gradients = tape.gradient(loss, variables)
  optimizer.apply_gradients(zip(gradients, variables))
  return loss

def evaluate_cset(infiller, cset, char2idx, max_length):
  """Evaluates given cognate set."""
  tgt_index = 0
  inputs = []
  input_mask = []
  # Find possible target positions
  for i, p in enumerate(cset):
    if p.strip():
      if p == '<TARGET>':
        tgt_index = i
        inputs.append([char2idx['<TARGET>']] * max_length)
        input_mask.append([0.0] * max_length)
      else:
        seq = [char2idx['<BOS>']] + [
            char2idx[c] if c in char2idx else char2idx['<UNK>']
            for c in p.split()
        ] + [char2idx['<EOS>']]
        template = [char2idx['<BLANK>']] * max_length
        for j in range(min(len(seq), max_length)):
          template[j] = seq[j]
        inputs.append(template)
        input_mask.append([1.0] * max_length)
    else:
      inputs.append([char2idx['<BLANK>']] * max_length)
      input_mask.append([0.0] * max_length)

  inputs = tf.constant([inputs], dtype='int32')
  input_mask = tf.constant([input_mask], dtype='float32')

  logits = infiller(inputs, input_mask, training=False)
  trow = tf.math.argmax(logits[0, tgt_index, :, :], axis=-1)
  return trow.numpy()

def silent_translate(infiller, cset, char2idx, max_length, idx2char):
  result = evaluate_cset(infiller, cset, char2idx, max_length)
  result = list(result)
  result = ' '.join([
      idx2char[x] for x in result if idx2char[x] not in
      ['<PAD>', '<EOS>', '<BOS>', '<UNK>', '<TARGET>', '<BLANK>']
  ])
  #print("RESULT",result,"\n")
  return result

# Commented out IPython magic to ensure Python compatibility.
# %pip install utils

# Commented out IPython magic to ensure Python compatibility.
# this is for MacOS 
# %pip install dgl -f https://data.dgl.ai/wheels/repo.html

import scipy.sparse as sp

def normalize_adj(adj, symmetric=True):
    if symmetric:
        d = sp.diags(np.power(np.array(adj.sum(1)), -0.5).flatten(), 0)
        a_norm = adj.dot(d).transpose().dot(d).tocsr()
    else:
        d = sp.diags(np.power(np.array(adj.sum(1)), -1).flatten(), 0)
        a_norm = d.dot(adj).tocsr()
    return a_norm


def normalize_adj_numpy(adj, symmetric=True):
    if symmetric:
        d = np.diag(np.power(np.array(adj.sum(1)), -0.5).flatten(), 0)
        a_norm = adj.dot(d).transpose().dot(d)
    else:
        d = np.diag(np.power(np.array(adj.sum(1)), -1).flatten(), 0)
        a_norm = d.dot(adj)
    return a_norm


def preprocess_adj(adj, symmetric=True):
    adj = adj + sp.eye(adj.shape[0])
    adj = normalize_adj(adj, symmetric)
    return adj


def preprocess_adj_numpy(adj, symmetric=True):
    adj = adj + np.eye(adj.shape[0])
    adj = normalize_adj_numpy(adj, symmetric)
    return adj


def preprocess_adj_tensor(adj_tensor, symmetric=True):
    adj_out_tensor = []
    for i in range(adj_tensor.shape[0]):
        adj = adj_tensor[i]
        adj = adj + np.eye(adj.shape[0])
        adj = normalize_adj_numpy(adj, symmetric)
        adj_out_tensor.append(adj)
    adj_out_tensor = np.array(adj_out_tensor)
    return adj_out_tensor

# Commented out IPython magic to ensure Python compatibility.
# %pip install scipy

from keras.layers import Dense, Activation, Dropout
from keras.models import Model, Sequential
from keras.regularizers import l2
from keras.optimizers import Adam
import keras.backend as K
import numpy as np

from keras_dgl.utils import *
from keras_dgl.layers import GraphCNN

def train_model():
  """Training pipeline."""


  # Produce base training data and vocab, and expand the training data.
  datadir = '/data_dir'
  cogsets, vocab = build_base_data_and_vocab()
  print(vocab)
  char2idx = {vocab[i]: i for i in range(len(vocab))}
  idx2char = {i: vocab[i] for i in range(len(vocab))}
  nlangs = len(cogsets[0])
  hparams = get_hparams()
  vocab_size = len(vocab)
  all_samples = expand_training_set(cogsets)

  # # Read in the dev data.
  # dev_sets = []
  # filepath = '/content/data_dir/dev-0.10_01.tsv'
  # with open(filepath, 'r', encoding='utf-8') as fin:
  #   # Skip header.
  #   next(fin)
  #   for line in fin:
  #     parts = tuple(line.strip('\n').split('\t')[1:])
  #     parts = ['<TARGET>' if p == '?' else p for p in parts]
  #     dev_sets.append(parts)

  # # Read in the dev  solution set.
  # dev_solutions = []
  # filepath = '/content/data_dir/dev_solutions-0.10_01.tsv'
  # with open(filepath, 'r', encoding='utf-8') as fin:
  #   # Skip header.
  #   next(fin)
  #   for line in fin:
  #     parts = tuple(line.strip('\n').split('\t')[1:])
  #     dev_solutions.append(''.join(parts).strip())

  # Core settings.
  steps_per_epoch = 500
  batch_size = 1
  max_length = 20
  # Have we written the vocab and hparams already?
  vocab_written = False

  # Define the model, optimizer and loss function.
  #infiller = Infiller(vocab_size, hparams, batch_size, nlangs, max_length)
  optimizer = tf.keras.optimizers.Adam()

  checkpoint_dir = '/checkpoint_dir'
  # if checkpoint_dir:
  #     # checkpoint_prefix = os.path.join(checkpoint_dir, 'ckpt')
  #     checkpoint = tf.train.Checkpoint(optimizer=optimizer, infiller=infiller)

  logging.info('Training the model ...')
  train_dataset = build_train_dataset(all_samples, batch_size, nlangs,
                                      max_length, char2idx)
  best_error = None

  SYM_NORM = True
  vocab_size = vocab_size
  embedding_dim = hparams['embedding_dim']
  embedding = tf.keras.layers.Embedding(vocab_size,
                                               embedding_dim)
  A_norm = preprocess_adj_numpy(A, SYM_NORM)
  num_filters = 2
  graph_conv_filters = np.concatenate([A_norm, np.matmul(A_norm, A_norm)], axis=0)
  graph_conv_filters = K.constant(graph_conv_filters)

  model = Sequential()
  model.add(GraphCNN(16, num_filters, graph_conv_filters, input_shape=(X.shape[1],), activation='elu', kernel_regularizer=l2(5e-4)))
  model.add(Dropout(0.2))
  model.add(GraphCNN(Y.shape[1], num_filters, graph_conv_filters, activation='elu', kernel_regularizer=l2(5e-4)))
  model.add(Activation('softmax'))
  model.compile(loss='categorical_crossentropy', optimizer=Adam(lr=0.01), metrics=['acc'])

  nb_epochs = 500
  for epoch in range(nb_epochs):
    start = time.time()

    total_loss = 0

    for (_, (inp, targ, inp_mask,
             targ_mask)) in enumerate(train_dataset.take(steps_per_epoch)):

      model.fit(inp, targ, sample_weight=inp_mask, batch_size=A.shape[0], epochs=1, shuffle=False, verbose=0)
      Y_pred = model.predict(inp, batch_size=A.shape[0])
      _, train_acc = evaluate_preds(Y_pred, [Y_train], [train_idx])
      _, test_acc = evaluate_preds(Y_pred, [Y_test], [test_idx])
      print("Epoch: {:04d}".format(epoch), "train_acc= {:.4f}".format(train_acc[0]), "test_acc= {:.4f}".format(test_acc[0]))

      # batch_loss = train_step(infiller, optimizer, inp, inp_mask, targ,
      #                         targ_mask)
      total_loss += test_acc
      # print("lossssssss",total_loss)

    print('Epoch {} Loss {:.4f}'.format(epoch + 1,
                                        total_loss / steps_per_epoch))

    print('Time taken for 1 epoch {} sec\n'.format(time.time() - start))

    # Evaluate on dev set:
    derrors = [0 for l in range(nlangs)]
    dtotals = [0 for l in range(nlangs)]
    allerrors = 0
    # for dset, dsol in zip(dev_sets, dev_solutions):
    #   tgt_index = dset.index('<TARGET>')
    #   dtotals[tgt_index] += 1
    #   pred = silent_translate(infiller, dset, char2idx, max_length, idx2char)
    #   if pred != dsol:
    #     derrors[tgt_index] += 1
    #     allerrors += 1
    # derrors = [x / y for x, y in zip(derrors, dtotals) if y != 0]
    # mean_accuracy = np.mean(derrors)

    # Update based on dev set.
    # if not best_error or mean_accuracy <= best_error:
    #   print('ERROR_UPDATE:', derrors)
    if checkpoint_dir:
      checkpoint.save('/checkpoint_dir/best_model.ckpt')
    #     # Write the vocab AFTER ensuring checkpoint dir has been created.
    if not vocab_written:
          # Write the model parameters.
       hparams = get_hparams()
       hparams['embedding_dim'] = hparams["embedding_dim"]
       hparams['kernel_width'] = hparams["kernel_width"]
       hparams['filters'] = hparams["filters"]
       hparams['dropout'] = hparams["dropout"]
       hparams['nonlinearity'] = hparams["nonlinearity"]
       hparams['sfactor'] = hparams["sfactor"]
       with open(checkpoint_dir + 'hparams.json', 'w') as vfile:
            json.dump(hparams, vfile)
    #       # Write the vocabulary.
       with open(
              checkpoint_dir + '/vocab.txt', 'w', encoding='utf-8') as vfile:
            for v in vocab:
              vfile.write(v + '\n')
    #       vocab_written = True
    #   best_error = mean_accuracy
    # print(best_error, mean_accuracy, '\n')

  # For some reason this step takes a couple of minutes to complete using
  # Tensorflow 2.8.0.
  logging.info('Done. Shutting down ...')

train_model()

def load_data(path="data_dir/", dataset="training-0.10"):
    """Load citation network dataset """
    print('Loading {} dataset...'.format(dataset))

    idx_features_labels = np.genfromtxt("{}{}.tsv".format(path, dataset))
    print(idx_features_labels.shape)
    features = sp.csr_matrix(idx_features_labels[:, 1:-1], dtype=np.float32)
    labels = encode_onehot(idx_features_labels[:, -1])

    # build graph
    idx = np.array(idx_features_labels[:, 0], dtype=np.int32)
    idx_map = {j: i for i, j in enumerate(idx)}
    edges_unordered = np.genfromtxt("{}{}.tsv".format(path, dataset), dtype=np.int32)
    edges = np.array(list(map(idx_map.get, edges_unordered.flatten())),
                     dtype=np.int32).reshape(edges_unordered.shape)
    adj = sp.coo_matrix((np.ones(edges.shape[0]), (edges[:, 0], edges[:, 1])),
                        shape=(labels.shape[0], labels.shape[0]), dtype=np.float32)

    # build symmetric adjacency matrix
    adj = adj + adj.T.multiply(adj.T > adj) - adj.multiply(adj.T > adj)

    # features = normalize_features(features)
    # adj = normalize_adj(adj + sp.eye(adj.shape[0]))

    print('Dataset has {} nodes, {} edges, {} features.'.format(adj.shape[0], edges.shape[0], features.shape[1]))

    return features.todense(), adj, labels

from keras.layers import Dense, Activation, Dropout
from keras.models import Model, Sequential
from keras.regularizers import l2
from keras.optimizers import Adam
import keras.backend as K
import numpy as np

from utils import *
from keras_dgl.layers import GraphCNN


# Prepare Data
X, A, Y = load_data(dataset='training-0.10')
A = np.array(A.todense())

_, Y_val, _, train_idx, val_idx, test_idx, train_mask = get_splits(Y)
train_idx = np.array(train_idx)
val_idx = np.array(val_idx)
test_idx = np.array(test_idx)
labels = np.argmax(Y, axis=1) + 1

# Normalize X
X /= X.sum(1).reshape(-1, 1)
X = np.array(X)

Y_train = np.zeros(Y.shape)
labels_train = np.zeros(labels.shape)
Y_train[train_idx] = Y[train_idx]
labels_train[train_idx] = labels[train_idx]

Y_test = np.zeros(Y.shape)
labels_test = np.zeros(labels.shape)
Y_test[test_idx] = Y[test_idx]
labels_test[test_idx] = labels[test_idx]

# Build Graph Convolution filters
SYM_NORM = True
A_norm = preprocess_adj_numpy(A, SYM_NORM)
num_filters = 2
graph_conv_filters = np.concatenate([A_norm, np.matmul(A_norm, A_norm)], axis=0)
graph_conv_filters = K.constant(graph_conv_filters)

# Build Model
model = Sequential()
model.add(GraphCNN(16, num_filters, graph_conv_filters, input_shape=(X.shape[1],), activation='elu', kernel_regularizer=l2(5e-4)))
model.add(Dropout(0.2))
model.add(GraphCNN(Y.shape[1], num_filters, graph_conv_filters, activation='elu', kernel_regularizer=l2(5e-4)))
model.add(Activation('softmax'))
model.compile(loss='categorical_crossentropy', optimizer=Adam(lr=0.01), metrics=['acc'])

nb_epochs = 500

for epoch in range(nb_epochs):
    model.fit(X, Y_train, sample_weight=train_mask, batch_size=A.shape[0], epochs=1, shuffle=False, verbose=0)
    Y_pred = model.predict(X, batch_size=A.shape[0])
    _, train_acc = evaluate_preds(Y_pred, [Y_train], [train_idx])
    _, test_acc = evaluate_preds(Y_pred, [Y_test], [test_idx])
    print("Epoch: {:04d}".format(epoch), "train_acc= {:.4f}".format(train_acc[0]), "test_acc= {:.4f}".format(test_acc[0]))

# Sample Output

def get_vocab(checkpoint_dir):
  file_path ="checkpoint_dir/vocab.txt"
  if not file_path:
    raise FileNotFoundError(f'File {file_path} does not exist')
  logging.info('Loading vocab from %s ...', file_path)
  with open(file_path, 'r', encoding='utf8') as f:
    vocab = [symbol.strip() for symbol in f if symbol]
  logging.info('%d symbols loaded.', len(vocab))
  return vocab

def decode_with_model():
  hparams = get_hparams()
  checkpoint_dir='checkpoint_dir/'
  vocab = get_vocab("checkpoint_dir/")
  char2idx = {vocab[i]: i for i in range(len(vocab))}
  idx2char = {i: vocab[i] for i in range(len(vocab))}
  vocab_size = len(vocab)
  batch_size = 1
  max_length = 20

  test_filepath = 'data_dir/test-0.10.tsv'
  preds_filepath = 'data_dir/pred-0.10.tsv'

  with open(test_filepath, 'r', encoding='utf-8') as fin:
    nlangs = len(next(fin).strip('\n').split('\t')) - 1


  latest_ckpt_path = tf.train.latest_checkpoint(checkpoint_dir)
  if not latest_ckpt_path:
    raise ValueError('No checkpoint available')
  logging.info('Restoring from checkpoint %s ...', latest_ckpt_path)
  infiller = Infiller(vocab_size, hparams, batch_size, nlangs, max_length)
  checkpoint = tf.train.Checkpoint(infiller=infiller)
  checkpoint.restore(latest_ckpt_path).expect_partial()

  logging.info('Generating predictions and saving results...')
  with open(preds_filepath, 'w', encoding='utf-8') as vfile:
    with open(test_filepath, 'r', encoding='utf-8') as tfile:
      # Copy the header.
      vfile.write(next(tfile))
      for line in tfile:
        parts = line.strip('\n').split('\t')
        tset = ['<TARGET>' if p == '?' else p for p in parts[1:]]
        tgt_index = tset.index('<TARGET>')
        pred = silent_translate(infiller, tset, char2idx, max_length, idx2char)
        row = ['' for p in parts]
        row[0] = parts[0]
        row[tgt_index + 1] = pred
        vfile.write('\t'.join(row) + '\n')

decode_with_model()

# Commented out IPython magic to ensure Python compatibility.
# %pip install lingrex
# %pip install lingpy

from lingrex.util import bleu_score
from lingpy import *
from lingpy.evaluate.acd import _get_bcubed_score as bcubed_score
from tabulate import tabulate
from collections import defaultdict
from lingpy.sequence.ngrams import get_n_ngrams
import math

def load_cognate_file(path):
    """
    Helper function for simplified cognate formats.
    """
    data = csv2list(path, strip_lines=False)
    header = data[0]
    languages = header[1:]
    out = {}
    sounds = defaultdict(lambda : defaultdict(list))
    for row in data[1:]:
        out[row[0]] = {}
        for language, entry in zip(languages, row[1:]):
            out[row[0]][language] = entry.split()
            for i, sound in enumerate(entry.split()):
                sounds[sound][language] += [[row[0], i]]
    # print("Languages:::::",languages)
    # print("Sound::::",sounds)
    # print("OUT::::",out)
    return languages,sounds, out

import math
def bleu_score(word, reference, n=4, weights=None, trim=False):
    """
    Compute the BLEU score for predicted word and reference.
    """

    if not weights:
        weights = [1 / n for x in range(n)]

    scores = []
    for i in range(1, n + 1):

        new_wrd = list(get_n_ngrams(word, i))
        new_ref = list(get_n_ngrams(reference, i))
        if trim and i > 1:
            new_wrd = new_wrd[i - 1 : -(i - 1)]
            new_ref = new_ref[i - 1 : -(i - 1)]

        clipped, divide = [], []
        for itm in set(new_wrd):
            clipped += [new_ref.count(itm)]
            divide += [new_wrd.count(itm)]
        scores += [sum(clipped) / sum(divide)]

    # calculate arithmetic mean
    out_score = 1
    for weight, score in zip(weights, scores):
        out_score = out_score * (score**weight)

    if len(word) > len(reference):
        bp = 1
    else:
        bp = math.e ** (1 - (len(reference) / len(word)))
    return bp * (out_score ** (1 / sum(weights)))

def compare_words(firstfile, secondfile, report=True):
    """
    Evaluate the predicted and attested words in two datasets.
    """

    (languages, soundsA, first), (languagesB, soundsB, last) = load_cognate_file(firstfile), load_cognate_file(secondfile)
    print("///",languages, soundsA, first)
    all_scores = []
    for language in languages:
        scores = []
        almsA, almsB = [], []
        for key in first:
            if language in first[key]:
                entryA = first[key][language]
                # print("@@@@",entryA)
                if " ".join(entryA):
                    try:
                        # print("&&&&",entryA)
                        entryB = last[key][language]
                        # print("####",entryB)
                    except KeyError:
                        print("Missing entry {0} / {1} / {2}".format(
                            key, language, secondfile))
                        entryB = ""
                    if not entryB:
                        entryB = (2 * len(entryA)) * ["??"]
                    # print(entryA)
                    # print(entryB)
                    almA, almB, _ = nw_align(entryA, entryB)
                    almsA += almA
                    almsB += almB
                    score = 0
                    for a, b in zip(almA, almB):
                        if a == b and a not in "???-":
                            pass
                        elif a != b:
                            score += 1
                    scoreD = score / len(almA)
                    bleu = bleu_score(entryA, entryB, n=4, trim=False)
                    scores += [[key, entryA, entryB, score, scoreD, bleu]]
        if scores:
            p, r = bcubed_score(almsA, almsB), bcubed_score(almsB, almsA)
            fs = 2 * (p*r) / (p+r)
            all_scores += [[
                language,
                sum([row[-3] for row in scores])/len(scores),
                sum([row[-2] for row in scores])/len(scores),
                fs,
                sum([row[-1] for row in scores])/len(scores)]]
    all_scores += [[
        "TOTAL", 
        sum([row[-4] for row in all_scores])/len(languages),
        sum([row[-3] for row in all_scores])/len(languages),
        sum([row[-2] for row in all_scores])/len(languages),
        sum([row[-1] for row in all_scores])/len(languages),
        ]]
    if report:
        print(
                tabulate(
                    all_scores, 
                    headers=[
                        "Language", "ED", "ED (Normalized)", 
                        "B-Cubed FS", "BLEU"], floatfmt=".3f"))
    return all_scores

# compare_words('/content/data_dir/pred-0.10.tsv','/content/data_dir/solutions-0.10.tsv')

compare_words('/content/data_dir/pred-0.10.tsv','/content/data_dir/solutions-0.10.tsv')

