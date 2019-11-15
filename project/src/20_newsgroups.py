from argparse import Namespace
import copy
import itertools
import logging
from pathlib import Path
# import pickle as pk
from typing import Set, Tuple, Union

import nltk.tokenize
import numpy as np
import sklearn.datasets
# noinspection PyProtectedMember
from sklearn.utils import Bunch

from torch.utils.data import Subset
import torchtext
from torchtext.data import Field, LabelField
import torchtext.datasets
from torchtext.data.dataset import Dataset
import torchtext.vocab

# Valid Choices - Any subset of: ('headers', 'footers', 'quotes')
from logger_utils import setup_logger

DATASET_REMOVE = ('headers', 'footers', 'quotes')
VALID_DATA_SUBSETS = ("train", "test", "all")

CACHE_DIR = None

DATA_COL = "data"
LABEL_COL = "target"
LABEL_NAMES = "target_names"

MAX_SEQ_LEN = 500

POS_LABEL = 1
UNLABELED = 0
NEG_LABEL = -1


def _download_20newsgroups(subset: str, data_dir: Path, pos_cls: Set[int], neg_cls: Set[int]):
    r"""
    Gets the specified \p subset of the 20 Newsgroups dataset.  If necessary, the dataset is
    downloaded to directory \p data_dir.  It also tokenizes the import dataset

    :param data_dir: Path to the directory to sore
    :param subset: Valid choices, "train", "test", and "all"
    :return: Dataset
    """
    assert not data_dir.is_file(), "Must be a directory"
    assert subset in VALID_DATA_SUBSETS, "Invalid data subset"

    data_dir.mkdir(parents=True, exist_ok=True)
    dataset = sklearn.datasets.fetch_20newsgroups(data_home=data_dir, shuffle=False,
                                                  remove=DATASET_REMOVE, subset=subset)

    _filter_by_classes(dataset, pos_classes=pos_cls, neg_classes=neg_cls)
    # dataset[DATA_COL] = [_tokenize(x) for x in dataset[DATA_COL]]
    return dataset


def _filter_by_classes(bunch: Bunch, pos_classes: Set[int], neg_classes: Set[int]):
    r""" Removes any dataset items not in the specified class label lists """
    all_classes = pos_classes | neg_classes
    # Filter according to positive and negative class set
    keep_idx = [val in all_classes for val in bunch[LABEL_COL]]
    assert any(keep_idx), "No elements to keep list"

    def _filt_col(col_name: str):
        return list(itertools.compress(bunch[col_name], keep_idx))

    for key in bunch.keys():
        if isinstance(bunch[key], (list, np.ndarray)): continue
        bunch[key] = _filt_col(key)


def _filter_bunch_by_idx(bunch: Bunch, keep_idx):
    r"""
    Filters \p Bunch object and removes any unneeded elements

    :param bunch: Dataset \p Bunch object to filter
    :param keep_idx: List of Boolean values where the value is \p True if the element should be
                     kept.
    :return: Filtered \p Bunch object
    """
    bunch = copy.deepcopy(bunch)
    for key, val in bunch.items():
        if not isinstance(val, (list, np.ndarray)): continue
        if len(keep_idx) == len(val): continue

        obj_type = type(val)

        bunch[key] = obj_type(itertools.compress(val, keep_idx))
    return bunch


def _remove_indices_from_dataset(ds: Union[Subset, Dataset], idx_to_remove: np.ndarray) -> Subset:
    r"""
    Remove the specified indices from dataset \p Dataset
    :param ds: Dataset to file
    :param idx_to_remove: Indices to remove from the dataset
    :return: Filtered dataset
    """
    remain_idx = set(range(len(ds))) - set(idx_to_remove)
    remain_idx = np.asarray(remain_idx, dtype=np.int32).sort()
    return Subset(ds, remain_idx)


def _select_positive_bunch(size_p: int, bunch: Bunch, pos_classes: Set[int],
                           remove_p_from_u: bool) -> Tuple[Bunch, Bunch]:
    r"""

    :param size_p: Number of (positive elements) to select from Bunch
    :param bunch: Training (unlabeled) \p Bunch
    :param pos_classes: List of positive classes in t
    :param remove_p_from_u: If \p True, elements in the positive (labeled) dataset are removed
                            from the unlabeled set.
    :return:
    """
    pos_idx = np.asarray([idx for idx, lbl in enumerate(bunch[LABEL_COL]) if lbl in pos_classes],
                         dtype=np.int32)

    assert len(pos_idx) >= size_p, "P set larger than the available data"
    np.random.shuffle(pos_idx)
    keep_idx = np.ones_like(bunch[LABEL_COL], dtype=np.bool)
    for idx in pos_idx[:size_p]: keep_idx[idx] = False

    p_bunch = _filter_bunch_by_idx(bunch, keep_idx)
    p_bunch[LABEL_NAMES] = [bunch[LABEL_NAMES][idx] for idx in sorted(list(pos_classes))]
    if remove_p_from_u:
        bunch = _filter_bunch_by_idx(bunch, ~keep_idx)
    return p_bunch, bunch


def _bunch_to_ds(bunch: Bunch, text: Field, label: LabelField) -> Dataset:
    r""" Converts the \p bunch to a classification dataset """
    fields = [('text', text), ('label', label)]
    examples = [torchtext.data.Example.fromlist([bunch[DATA_COL], bunch[LABEL_COL]], fields)]
    return Dataset(examples, fields)


def _print_stats(text: Field, label: LabelField):
    logging.info(f"Length of Text Vocabulary: {str(len(text.vocab))}")
    logging.info(f"Vector size of Text Vocabulary: {text.vocab.vectors.shape[1]}")
    logging.info("Label Length: " + str(len(label.vocab)))


def load_20newsgroups(args: Namespace, data_dir: Union[Path, str], pos_cls: Set[int],
                      neg_cls: Set[int]):
    r"""

    :param args: Parsed command line arguments
    :param data_dir: Directory from which to get the training data
    :param pos_cls: Set of labels for the POSITIVE classes
    :param neg_cls: Set of labels for the NEGATIVE classes
    :return: Train and test datasets
    """
    assert pos_cls and neg_cls, "Class list empty"
    assert not (pos_cls & neg_cls), "Positive and negative classes not disjoint"

    data_dir = Path(data_dir)

    global CACHE_DIR
    CACHE_DIR = data_dir / ".vector_cache"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    complete_train = _download_20newsgroups("train", data_dir, pos_cls, neg_cls)

    tokenizer = nltk.tokenize.word_tokenize
    # noinspection PyPep8Naming
    TEXT = Field(sequential=True, tokenize=tokenizer, lower=True, include_lengths=True,
                 fix_length=MAX_SEQ_LEN)
    # noinspection PyPep8Naming
    LABEL = LabelField(sequential=False)
    complete_ds = _bunch_to_ds(complete_train, TEXT, LABEL)
    TEXT.build_vocab(complete_ds,
                     vectors=torchtext.vocab.GloVe(name="6B", dim=args.embed_dim, cache=CACHE_DIR))
    LABEL.build_vocab(complete_ds)

    p_bunch, u_bunch = _select_positive_bunch(args.size_p, complete_train, pos_cls,
                                              remove_p_from_u=False)

    # # sequential: Not sequential labeling
    # test_bunch = _download_20newsgroups("test", data_dir, pos_cls, neg_cls)
    #
    _print_stats(TEXT, LABEL)
    return TEXT, LABEL, p_bunch, u_bunch


# def _fix_final_labels(ds: Union[Dataset, Subset], pos_classes: Set[int]) -> Dataset:
#     items = []
#     for data, lbl in ds:
#         lbl = POS_LABEL if lbl in pos_classes else NEG_LABEL
#         items =
#     return torchtext.datasets.TextClassificationDataset(vocab, filt_ds, all_labels))
#
#
# def _get_field():
#     import torchtext.data
#     f = torchtext.data.field
#

def _main():
    pos_classes = set(range(0, 10))
    neg_classes = set(range(10, 20))

    class Object:
        pass

    args = Object()
    args.size_p = 1000
    args.embed_dim = 300

    pk_file = Path("ds_debug.pk")
    if not pk_file.exists():
        # noinspection PyTypeChecker,PyUnusedLocal
        newsgroups = load_20newsgroups(args, "data", pos_classes, neg_classes)
        # with open(str(pk_file), "wb+") as f_out:
        #     pk.dump((train_ds, test_ds), f_out)
    else:
        pass
        # with open(str(pk_file), "rb") as f_in:
        #     train_ds, test_ds = pk.load(f_in)

    print("Completed")


if __name__ == "__main__":
    setup_logger()
    _main()