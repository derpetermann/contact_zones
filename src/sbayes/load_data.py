#!/usr/bin/env python3
# -*- coding: utf-8 -*-

""" Imports the real world data """

from __future__ import absolute_import, division, print_function, unicode_literals

import logging

from sbayes.util import read_features_from_csv
from sbayes.preprocessing import (compute_network,
                               read_inheritance_counts,
                               read_universal_counts)


class Data:
    def __init__(self, experiment):

        self.path_results = experiment.path_results
        self.experiment_name = experiment.experiment_name

        # File locations
        self.features_file = experiment.config['data']['FEATURES']
        self.universal_counts_file = experiment.config['data']['PRIOR']['universal']
        self.inheritance_counts_files = experiment.config['data']['PRIOR']['inheritance']

        # Features to be imported
        self.sites = None
        self.site_names = None
        self.features = None
        self.feature_names = None
        self.state_names = None
        self.families = None
        self.family_names = None
        self.network = None

        # Logs
        self.log_load_features = None
        self.log_load_universal_counts = None
        self.log_load_inheritance_counts = None

        # Priors to be imported
        self.prior_universal = {}
        self.prior_inheritance = {}

    def load_features(self):
        self.sites, self.site_names, self.features, self.feature_names, \
            self.state_names, self.families, self.family_names, self.log_load_features = \
            read_features_from_csv(file=self.features_file)
        self.network = compute_network(self.sites)

    def load_universal_counts(self):

        counts, states, self.log_load_universal_counts = \
            read_universal_counts(feature_names=self.feature_names,
                                  category_names=self.state_names,
                                  file=self.universal_counts_file)

        self.prior_universal = {'counts': counts,
                                'states': states}

    def load_inheritance_counts(self):
        counts, states, self.log_load_inheritance_counts = \
            read_inheritance_counts(family_names=self.family_names,
                                    feature_names=self.feature_names,
                                    category_names=self.state_names,
                                    files=self.inheritance_counts_files)
        self.prior_inheritance = {'counts': counts,
                                  'states': states}

    def log_loading(self):
        log_path = self.path_results + 'experiment.log'
        logging.basicConfig(format='%(message)s', filename=log_path, level=logging.DEBUG)
        logging.getLogger().addHandler(logging.StreamHandler())
        logging.info("\n")
        logging.info("DATA IMPORT")
        logging.info("##########################################")
        logging.info(self.log_load_features)
        logging.info(self.log_load_universal_counts)
        logging.info(self.log_load_inheritance_counts)
