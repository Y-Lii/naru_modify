"""Model training."""
import argparse
import os
import time
import tracemalloc

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import common
import datasets
import made
import transformer
from collections import Counter
import pandas as pd
import sys
import pickle

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Device', DEVICE)

parser = argparse.ArgumentParser()

# Training.
parser.add_argument('--datadir', type=str, default='dmv-tiny', help='Datadir.')
parser.add_argument('--edgedir', type=str, default='yago_edge', help='edgedir.')
parser.add_argument('--match', type=bool, default=False, help='match.')
parser.add_argument('--ratio', type=float, default=0.1, help='ratio.')
parser.add_argument('--num-gpus', type=int, default=0, help='#gpus.')
parser.add_argument('--bs', type=int, default=1024, help='Batch size.')
parser.add_argument(
    '--warmups',
    type=int,
    default=0,
    help='Learning rate warmup steps.  Crucial for Transformer.')
parser.add_argument('--epochs',
                    type=int,
                    default=20,
                    help='Number of epochs to train for.')
parser.add_argument('--constant-lr',
                    type=float,
                    default=None,
                    help='Constant LR?')
parser.add_argument(
    '--column-masking',
    action='store_true',
    help='Column masking training, which permits wildcard skipping' \
         ' at querying time.')

# MADE.
parser.add_argument('--fc-hiddens',
                    type=int,
                    default=128,
                    help='Hidden units in FC.')
parser.add_argument('--layers', type=int, default=4, help='# layers in FC.')
parser.add_argument('--residual', action='store_true', help='ResMade?')
parser.add_argument('--direct-io', action='store_true', help='Do direct IO?')
parser.add_argument(
    '--inv-order',
    action='store_true',
    help='Set this flag iff using MADE and specifying --order. Flag --order ' \
         'lists natural indices, e.g., [0 2 1] means variable 2 appears second.' \
         'MADE, however, is implemented to take in an argument the inverse ' \
         'semantics (element i indicates the position of variable i).  Transformer' \
         ' does not have this issue and thus should not have this flag on.')
parser.add_argument(
    '--input-encoding',
    type=str,
    default='binary',
    help='Input encoding for MADE/ResMADE, {binary, one_hot, embed}.')
parser.add_argument(
    '--output-encoding',
    type=str,
    default='one_hot',
    help='Iutput encoding for MADE/ResMADE, {one_hot, embed}.  If embed, '
         'then input encoding should be set to embed as well.')

# Transformer.
parser.add_argument(
    '--heads',
    type=int,
    default=0,
    help='Transformer: num heads.  A non-zero value turns on Transformer' \
         ' (otherwise MADE/ResMADE).'
)
parser.add_argument('--blocks',
                    type=int,
                    default=2,
                    help='Transformer: num blocks.')
parser.add_argument('--dmodel',
                    type=int,
                    default=32,
                    help='Transformer: d_model.')
parser.add_argument('--dff', type=int, default=128, help='Transformer: d_ff.')
parser.add_argument('--transformer-act',
                    type=str,
                    default='gelu',
                    help='Transformer activation.')

# Ordering.
parser.add_argument('--num-orderings',
                    type=int,
                    default=1,
                    help='Number of orderings.')
parser.add_argument(
    '--order',
    nargs='+',
    type=int,
    required=False,
    help=
    'Use a specific ordering.  ' \
    'Format: e.g., [0 2 1] means variable 2 appears second.'
)

parser.add_argument('--compression', type=bool, default=True, help='compression')
args = parser.parse_args()


def Entropy(name, data, bases=None):
    import scipy.stats
    s = 'Entropy of {}:'.format(name)
    ret = []
    for base in bases:
        assert base == 2 or base == 'e' or base is None
        e = scipy.stats.entropy(data, base=base if base != 'e' else None)
        ret.append(e)
        unit = 'nats' if (base == 'e' or base is None) else 'bits'
        s += ' {:.4f} {}'.format(e, unit)
    print(s)
    return ret


def RunEpoch(split,
             model,
             opt,
             train_data,
             val_data=None,
             batch_size=100,
             upto=None,
             epoch_num=None,
             verbose=False,
             log_every=10,
             return_losses=False,
             table_bits=None):
    torch.set_grad_enabled(split == 'train')
    model.train() if split == 'train' else model.eval()
    dataset = train_data if split == 'train' else val_data
    losses = []

    loader = torch.utils.data.DataLoader(dataset,
                                         batch_size=batch_size,
                                         shuffle=(split == 'train'))

    # How many orderings to run for the same batch?
    nsamples = 1
    if hasattr(model, 'orderings'):
        nsamples = len(model.orderings)

    for step, xb in enumerate(loader):
        if split == 'train':
            base_lr = 8e-4
            for param_group in opt.param_groups:
                if args.constant_lr:
                    lr = args.constant_lr
                elif args.warmups:
                    t = args.warmups
                    d_model = model.embed_size
                    global_steps = len(loader) * epoch_num + step + 1
                    lr = (d_model ** -0.5) * min(
                        (global_steps ** -.5), global_steps * (t ** -1.5))
                else:
                    lr = 1e-2

                param_group['lr'] = lr

        if upto and step >= upto:
            break

        xb = xb.to(DEVICE).to(torch.float32)

        # Forward pass, potentially through several orderings.
        xbhat = None
        model_logits = []
        num_orders_to_forward = 1
        if split == 'test' and nsamples > 1:
            # At test, we want to test the 'true' nll under all orderings.
            num_orders_to_forward = nsamples

        for i in range(num_orders_to_forward):
            if hasattr(model, 'update_masks'):
                # We want to update_masks even for first ever batch.
                model.update_masks()

            model_out = model(xb)
            model_logits.append(model_out)
            if xbhat is None:
                xbhat = torch.zeros_like(model_out)
            xbhat += model_out

        if xbhat.shape == xb.shape:
            if mean:
                xb = (xb * std) + mean
            loss = F.binary_cross_entropy_with_logits(
                xbhat, xb, size_average=False) / xbhat.size()[0]
        else:
            if model.input_bins is None:
                # NOTE: we have to view() it in this order due to the mask
                # construction within MADE.  The masks there on the output unit
                # determine which unit sees what input vars.
                xbhat = xbhat.view(-1, model.nout // model.nin, model.nin)
                # Equivalent to:
                loss = F.cross_entropy(xbhat, xb.long(), reduction='none') \
                    .sum(-1).mean()
            else:
                if num_orders_to_forward == 1:
                    loss = model.nll(xbhat, xb).mean()
                else:
                    # Average across orderings & then across minibatch.
                    #
                    #   p(x) = 1/N sum_i p_i(x)
                    #   log(p(x)) = log(1/N) + log(sum_i p_i(x))
                    #             = log(1/N) + logsumexp ( log p_i(x) )
                    #             = log(1/N) + logsumexp ( - nll_i (x) )
                    #
                    # Used only at test time.
                    logps = []  # [batch size, num orders]
                    assert len(model_logits) == num_orders_to_forward, len(
                        model_logits)
                    for logits in model_logits:
                        # Note the minus.
                        logps.append(-model.nll(logits, xb))
                    logps = torch.stack(logps, dim=1)
                    logps = logps.logsumexp(dim=1) + torch.log(
                        torch.tensor(1.0 / nsamples, device=logps.device))
                    loss = (-logps).mean()

        losses.append(loss.item())

        if step % log_every == 0:
            if split == 'train':
                print(
                    'Epoch {} Iter {}, {} entropy gap {:.4f} bits (loss {:.3f}, data {:.3f}) {:.5f} lr'
                        .format(epoch_num, step, split,
                                loss.item() / np.log(2) - table_bits,
                                loss.item() / np.log(2), table_bits, lr))
            else:
                print('Epoch {} Iter {}, {} loss {:.4f} nats / {:.4f} bits'.
                      format(epoch_num, step, split, loss.item(),
                             loss.item() / np.log(2)))

        if split == 'train':
            opt.zero_grad()
            loss.backward()
            opt.step()

        if verbose:
            print('%s epoch average loss: %f' % (split, np.mean(losses)))

        torch.cuda.empty_cache()

    if return_losses:
        return losses
    return np.mean(losses)


def ReportModel(model, blacklist=None):
    ps = []
    for name, p in model.named_parameters():
        if blacklist is None or blacklist not in name:
            ps.append(np.prod(p.size()))
    num_params = sum(ps)
    mb = num_params * 4 / 1024 / 1024
    print('Number of model parameters: {} (~= {:.1f}MB)'.format(num_params, mb))
    print(model)
    return mb


def InvertOrder(order):
    if order is None:
        return None
    # 'order'[i] maps nat_i -> position of nat_i
    # Inverse: position -> natural idx.  This it the 'true' ordering -- it's how
    # heuristic orders are generated + (less crucially) how Transformer works.
    nin = len(order)
    inv_ordering = [None] * nin
    for natural_idx in range(nin):
        inv_ordering[order[natural_idx]] = natural_idx
    return inv_ordering


def MakeMade(scale, cols_to_train, seed, fixed_ordering=None):
    if args.inv_order:
        print('Inverting order!')
        fixed_ordering = InvertOrder(fixed_ordering)

    model = made.MADE(
        nin=len(cols_to_train),
        hidden_sizes=[scale] *
                     args.layers if args.layers > 0 else [512, 256, 512, 128, 1024],
        nout=sum([c.DistributionSize() for c in cols_to_train]),
        input_bins=[c.DistributionSize() for c in cols_to_train],
        input_encoding=args.input_encoding,
        output_encoding=args.output_encoding,
        embed_size=32,
        seed=seed,
        do_direct_io_connections=args.direct_io,
        natural_ordering=False if seed is not None and seed != 0 else True,
        residual_connections=args.residual,
        fixed_ordering=fixed_ordering,
        column_masking=args.column_masking,
    ).to(DEVICE)

    return model


def MakeTransformer(cols_to_train, fixed_ordering, seed=None):
    return transformer.Transformer(
        num_blocks=args.blocks,
        d_model=args.dmodel,
        d_ff=args.dff,
        num_heads=args.heads,
        nin=len(cols_to_train),
        input_bins=[c.DistributionSize() for c in cols_to_train],
        use_positional_embs=True,
        activation=args.transformer_act,
        fixed_ordering=fixed_ordering,
        column_masking=args.column_masking,
        seed=seed,
    ).to(DEVICE)


def InitWeight(m):
    if type(m) == made.MaskedLinear or type(m) == nn.Linear:
        nn.init.xavier_uniform_(m.weight)
        nn.init.zeros_(m.bias)
    if type(m) == nn.Embedding:
        nn.init.normal_(m.weight, std=0.02)


def Bhattacharyya(p, q):
    BC = np.sum(np.sqrt(p * q))

    # Hellinger distance:
    #     h=np.sqrt(1-BC)

    # Papid:
    BC = max(BC, sys.float_info.min)
    b = -np.log(BC)
    return b


def distance(count_subs, count_obs):
    alls = set(count_subs.keys()) | set(count_obs.keys())
    count_left = []
    count_right = []
    for idx in alls:
        if idx in count_subs:
            count_left.append(count_subs[idx])
        else:
            count_left.append(0)

        if idx in count_obs:
            count_right.append(count_obs[idx])
        else:
            count_right.append(0)

    vals = np.asarray(count_left)
    val_left = vals / np.sum(vals)
    vals = np.asarray(count_right)
    val_right = np.asarray(vals) / np.sum(vals)

    return Bhattacharyya(val_left, val_right)


def train_model(table, name, seed=0):
    table_bits = Entropy(
        table,
        table.data.drop_duplicates().shape[0] * [1], [2])[0]
    fixed_ordering = None

    if args.order is not None:
        print('Using passed-in order:', args.order)
        fixed_ordering = args.order

    print(table.data.info())

    table_train = table

    model = MakeMade(
        scale=args.fc_hiddens,
        cols_to_train=table.columns,
        seed=seed,
        fixed_ordering=fixed_ordering,
    )

    mb = ReportModel(model)

    if not isinstance(model, transformer.Transformer):
        print('Applying InitWeight()')
        model.apply(InitWeight)

    if isinstance(model, transformer.Transformer):
        opt = torch.optim.Adam(
            list(model.parameters()),
            2e-4,
            betas=(0.9, 0.98),
            eps=1e-9,
        )
    else:
        opt = torch.optim.Adam(list(model.parameters()), 2e-4)

    bs = args.bs
    log_every = 200

    train_data = common.TableDataset(table_train)

    train_losses = []
    train_start = time.time()
    for epoch in range(args.epochs):

        mean_epoch_train_loss = RunEpoch('train',
                                         model,
                                         opt,
                                         train_data=train_data,
                                         val_data=train_data,
                                         batch_size=bs,
                                         epoch_num=epoch,
                                         log_every=log_every,
                                         table_bits=table_bits)

        if epoch % 1 == 0:
            print('epoch {} train loss {:.4f} nats / {:.4f} bits'.format(
                epoch, mean_epoch_train_loss,
                mean_epoch_train_loss / np.log(2)))
            since_start = time.time() - train_start
            print('time since start: {:.1f} secs'.format(since_start))

        train_losses.append(mean_epoch_train_loss)

    print('Training done; evaluating likelihood on full data:')
    all_losses = RunEpoch('test',
                          model,
                          train_data=train_data,
                          val_data=train_data,
                          opt=None,
                          batch_size=1024,
                          log_every=500,
                          table_bits=table_bits,
                          return_losses=True)
    model_nats = np.mean(all_losses)
    model_bits = model_nats / np.log(2)
    model.model_bits = model_bits

    if fixed_ordering is None:
        if seed is not None:
            PATH = 'models/{}-{:.1f}MB-model{:.3f}-data{:.3f}-{}-{}epochs-seed{}.pt'.format(
                name, mb, model.model_bits, table_bits, model.name(),
                args.epochs, seed)
        else:
            PATH = 'models/{}-{:.1f}MB-model{:.3f}-data{:.3f}-{}-{}epochs-seed{}-{}.pt'.format(
                name, mb, model.model_bits, table_bits, model.name(),
                args.epochs, seed, time.time())
    else:
        annot = ''
        if args.inv_order:
            annot = '-invOrder'

        PATH = 'models/{}-{:.1f}MB-model{:.3f}-data{:.3f}-{}-{}epochs-seed{}-order{}{}.pt'.format(
            args.dataset, mb, model.model_bits, table_bits, model.name(),
            args.epochs, seed, '_'.join(map(str, fixed_ordering)), annot)
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    torch.save(model.state_dict(), PATH)
    print('Saved to:')
    print(PATH)


def TrainTask(seed=0):
    torch.manual_seed(0)
    np.random.seed(0)

    node_files = os.listdir(args.datadir)
    edge_files = os.listdir(args.edgedir)
    df_index = {}

    counter_dict = {}
    for datafile in edge_files:
        key = datafile.split('.')[0]
        file = os.path.join(args.edgedir, datafile)
        df = pd.read_csv(file, usecols=[0, 2], escapechar="\\")
        df_index[key] = df
        counter_dict[key] = [dict(Counter(df['subject'])), dict(Counter(df['object']))]

    s = time.time()
    for datafile in node_files:
        name = datafile.split('.')[0]
        file = os.path.join(args.datadir, datafile)
        left = pd.read_csv(file, index_col='index', escapechar="\\")
        table = common.CsvTable(name, left, None, do_compression=args.compression, if_eval=False)
        train_model(table, name)
        for key in df_index:
            df = df_index[key]
            if args.match:
                items = key.split('_')
                if name == items[0]:
                    print(name, key)
                    tmp = pd.merge(left, df, how='inner', left_index=True, right_on='subject')
                    if tmp.shape[0] > 999:
                        table = common.CsvTable(name + '__' + key + '__1', tmp, None, do_compression=args.compression,
                                                if_eval=False)
                        train_model(table, name + '__' + key + '__1')

                if name == items[1]:
                    tmp = pd.merge(left, df, how='inner', left_index=True, right_on='object')
                    if tmp.shape[0] > 999:
                        table = common.CsvTable(key + '__' + name + '__2', tmp, None, do_compression=args.compression,
                                                if_eval=False)
                        train_model(table, key + '__' + name + '__2')

            else:
                tmp = pd.merge(left, df, how='inner', left_index=True, right_on='subject')

                dlist = []
                if 'subject' in tmp.columns:
                    dlist.append('subject')
                if 'subject_y' in tmp.columns:
                    dlist.append('subject_y')
                if dlist:
                    tmp.drop(dlist, axis=1, inplace=True)

                if tmp.shape[0] > 999:
                    table = common.CsvTable(name + '__' + key + '__1', tmp, None, do_compression=args.compression, if_eval=False)
                    train_model(table, name + '__' + key + '__1')

                tmp = pd.merge(left, df, how='inner', left_index=True, right_on='object')
                if tmp.shape[0] > 999:
                    table = common.CsvTable(key + '__' + name + '__2', tmp, None, do_compression=args.compression, if_eval=False)
                    train_model(table, key + '__' + name + '__2')

    print('Train type&predicate models took {:.1f}s'.format(time.time() - s))

    if counter_dict:
        predicates = list(counter_dict.keys())
        num = len(predicates)
        combinations = []
        chain_distance = []
        star_distance = []
        kleene_star = []
        s = time.time()
        if os.path.exists(os.path.join(os.getcwd(), 'keys')):
            # noinspection PyBroadException
            try:
                with open("keys", "rb") as fp:  # Unpickling
                    combinations = pickle.load(fp)
                with open("chain_dis", "rb") as fp:  # Unpickling
                    chain_distance = pickle.load(fp)
                with open("star_dis", "rb") as fp:  # Unpickling
                    star_distance = pickle.load(fp)
            except Exception as e:
                print("Need to calculate distance")
        else:
            for i in range(num):
                print("Calculating distance of " + predicates[i] + " ...")
                left = counter_dict[predicates[i]]
                # kleene_star.append(distance(left[0], left[1]))
                for j in range(i + 1, num):
                    right = counter_dict[predicates[j]]
                    if args.match:
                        cp1 = predicates[i].split('_')
                        cp2 = predicates[j].split('_')
                        add_key = False
                        chain_tmp = [-1, -1]
                        if cp1[0] == cp2[0]:
                            star_distance.append(distance(left[0], right[0]))
                            add_key = True
                        if cp1[1] == cp2[0]:
                            chain_tmp[0] = distance(left[1], right[0])
                            add_key = True
                        if cp1[0] == cp2[1]:
                            chain_tmp[1] = distance(right[1], left[0])
                            add_key = True

                        if add_key:
                            print(predicates[i] + '&' + predicates[j])
                            combinations.append(predicates[i] + '&' + predicates[j])
                            chain_distance += chain_tmp
                    else:
                        combinations.append(predicates[i] + '&' + predicates[j])
                        chain_distance += [distance(left[1], right[0]), distance(right[1], left[0])]
                        # star_distance += [distance(left[0], right[0]), distance(left[1], right[1])]
                        star_distance.append(distance(left[0], right[0]))
            print('Calculating distance took {:.1f}s'.format(time.time() - s))
            # save results
            with open("keys", "wb") as fp:  # Pickling
                pickle.dump(combinations, fp)
            with open("chain_dis", "wb") as fp:  # Pickling
                pickle.dump(chain_distance, fp)
            with open("star_dis", "wb") as fp:  # Pickling
                pickle.dump(star_distance, fp)

        chain_argsort = np.argsort(chain_distance)
        star_argsort = np.argsort(star_distance)
        kleene_argsort = np.argsort(kleene_star)

        # for those pairs having shorter distance, we assume indepedence between them
        chain_index = chain_argsort[int(len(chain_argsort) * args.ratio):]
        star_index = star_argsort[int(len(star_argsort) * args.ratio):]
        # kleene_index = kleene_argsort[int(len(kleene_argsort) * args.ratio) :]

        s = time.time()
        cnt = 0
        rcd = []
        chain_shape = {}
        for idx in chain_index[::-1]:
            if chain_distance[idx] == -1:
                break
            key = combinations[int(idx / 2)]
            rmd = idx % 2
            # generate training dataset
            first = key.split('&')[0]
            second = key.split('&')[1]
            left = df_index[first]
            right = df_index[second]

            # if ('isCitizenOf' in key) or ('isLocatedIn' in key):
            #     rcd.append(key)
            #     print("Pass...." + key)
            #     continue

            if rmd == 0:
                df = pd.merge(left, right, how='inner', left_on='object', right_on='subject')
                name = first + '__' + second + '__3'
            else:
                df = pd.merge(left, right, how='inner', left_on='subject', right_on='object')
                name = second + '__' + first + '__3'
            # train model
            nuniq = [df.shape[0]]
            nuniq += list(df.nunique())
            chain_shape[name] = nuniq
            # if df.shape[0] > 999:
            #     print("Chain " + name)
            #     table = common.CsvTable(name, df, None, do_compression=args.compression, if_eval=False)
            #     train_model(table, name)
            cnt += 1

        print('Train {} chain-shape models took {:.1f}s'.format(cnt, time.time() - s))
        s = time.time()

        cnt = 0
        star_shape = {}
        for idx in star_index[::-1]:
            if star_distance[idx] == -1:
                break
            key = combinations[idx]
            # rmd = idx % 2
            # generate training dataset
            first = key.split('&')[0]
            second = key.split('&')[1]
            left = df_index[first]
            right = df_index[second]
            df = pd.merge(left, right, how='inner', left_on='subject', right_on='subject')
            name = first + '__' + second + '__4'
            # train model
            # if df.shape[0] > 999:
            #     print("Star " + name)
            #     table = common.CsvTable(name, df, None, do_compression=args.compression, if_eval=False)
            #     train_model(table, name)
            nuniq = [df.shape[0]]
            nuniq += list(df.nunique())
            star_shape[name] = nuniq
            cnt += 1

        print('Train {} star-shape models took {:.1f}s'.format(cnt, time.time() - s))

        if chain_shape:
            with open("chain_shape", "wb") as fp:  # Pickling
                pickle.dump(chain_shape, fp)
        if star_shape:
            with open("star_shape", "wb") as fp:  # Pickling
                pickle.dump(star_shape, fp)
        # for key in rcd:
        #     print(key)


tracemalloc.start()

TrainTask()

print(tracemalloc.get_traced_memory())

tracemalloc.stop()
