"""Dataset registrations."""
import os

import numpy as np

import common


def LoadDmv(filename='Vehicle__Snowmobile__and_Boat_Registrations', do_compression=True, if_eval=False):
    csv_file = './datasets/{}'.format(filename)
    cols = [
        'id', 'movie_id', 'linked_movie_id', 'link_type_id'
    ]
    # Note: other columns are converted to objects/strings automatically.  We
    # don't need to specify a type-cast for those because the desired order
    # there is the same as the default str-ordering (lexicographical).
    type_casts = {'birth_date': np.datetime64, 'death_date': np.datetime64, 'release dates': np.datetime64}
    return common.CsvTable(filename, csv_file, None, type_casts, do_compression=do_compression, if_eval=if_eval)
