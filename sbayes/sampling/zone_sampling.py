#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import random as _random
from copy import deepcopy

import numpy as np

from sbayes.sampling.mcmc_generative import MCMCGenerative
from sbayes.util import get_neighbours, normalize, dirichlet_pdf, get_max_size_list
from sbayes.preprocessing import sample_categorical


class IndexSet(set):

    def __init__(self, all_i=True):
        super().__init__()
        self.all = all_i

    def add(self, element):
        super(IndexSet, self).add(element)

    def clear(self):
        super(IndexSet, self).clear()
        self.all = False

    def __bool__(self):
        return self.all or (len(self) > 0)

    def __copy__(self):
        # other = deepcopy(super(IndexSet, self))
        other = IndexSet(all_i=self.all)
        for element in self:
            other.add(element)

        return other

    def __deepcopy__(self, memo):
        # other = deepcopy(super(IndexSet, self))
        other = IndexSet(all_i=self.all)
        for element in self:
            other.add(deepcopy(element))

        return other


class Sample(object):
    """
    Attributes:
        chain (int): index of the MC3 chain.
        zones (np.array): Assignment of sites to zones.
            shape: (n_zones, n_sites)
        weights (np.array): Weights of zone, family and global likelihood for different features.
            shape: (n_features, 3)
        p_global (np.array): Global probabilities of categories
            shape(n_features, n_categories)
        p_zones (np.array): Probabilities of categories in zones
            shape: (n_zones, n_features, n_categories)
        p_families (np.array): Probabilities of categories in families
            shape: (n_families, n_features, n_categories)
        source (np.array): Assignment of single observations (a feature in a language) to be
                           the result of the global, zone or family distribution.
            shape: (n_sites, n_features, 3)
    """

    def __init__(self, zones, weights, p_global, p_zones, p_families, source=None, chain=0):
        self.zones = zones
        self.weights = weights
        self.p_global = p_global
        self.p_zones = p_zones
        self.p_families = p_families

        self.source = source

        self.chain = chain

        # The sample contains information about which of its parameters was changed in the last MCMC step
        self.what_changed = {}
        self.everything_changed()

    def everything_changed(self):
        self.what_changed = {'lh': {'zones': IndexSet(), 'weights': True,
                                    'p_global': IndexSet(), 'p_zones': IndexSet(), 'p_families': IndexSet()},
                             'prior': {'zones': IndexSet(), 'weights': True,
                                       'p_global': IndexSet(), 'p_zones': IndexSet(), 'p_families': IndexSet()}}

    def copy(self):
        zone_copied = deepcopy(self.zones)
        weights_copied = deepcopy(self.weights)
        what_changed_copied = deepcopy(self.what_changed)

        def maybe_copy(obj):
            if obj is not None:
                return obj.copy()

        p_global_copied = maybe_copy(self.p_global)
        p_zones_copied = maybe_copy(self.p_zones)
        p_families_copied = maybe_copy(self.p_families)
        source_copied = maybe_copy(self.source)

        new_sample = Sample(chain=self.chain, zones=zone_copied, weights=weights_copied,
                            p_global=p_global_copied, p_zones=p_zones_copied, p_families=p_families_copied,
                            source=source_copied)

        new_sample.what_changed = what_changed_copied

        return new_sample


class ZoneMCMC(MCMCGenerative):
    """float: Probability at which grow operator only considers neighbours to add to the zone."""

    def __init__(self, var_proposal, p_grow_connected,
                 initial_sample, initial_size,
                 **kwargs):

        super(ZoneMCMC, self).__init__(**kwargs)

        # Data
        self.features = self.data.features
        self.n_features = self.features.shape[1]
        self.applicable_states = self.data.states
        self.n_states_by_feature = np.sum(self.data.states, axis=-1)

        # Network
        self.network = self.data.network
        self.adj_mat = self.network['adj_mat']
        self.locations = self.network['locations']

        # Sampling
        self.n_sites = self.adj_mat.shape[0]
        self.p_grow_connected = p_grow_connected

        # Zone size /initial sample
        self.n_zones = self.model.n_zones
        self.min_size = self.model.min_size
        self.max_size = self.model.max_size
        self.initial_sample = initial_sample
        self.initial_size = initial_size

        # Is inheritance (information on language families) available?
        self.inheritance = self.model.inheritance
        self.families = self.data.families

        # Families
        if self.inheritance:
            self.n_families = self.families.shape[0]

        # Variance of the proposal distribution
        self.var_proposal_weight = var_proposal['weights']
        self.var_proposal_p_global = var_proposal['universal']
        self.var_proposal_p_zones = var_proposal['contact']
        try:
            self.var_proposal_p_families = var_proposal['inheritance']
        except KeyError:
            pass

        # todo remove after testing
        self.q_areas_stats = {'q_grow': [],
                              'q_back_grow': [],
                              'q_shrink': [],
                              'q_back_shrink': []
                              }

    def gibbs_sample_sources(self, sample: Sample):
        """Resample the of observations to mixture components (their source).

        Args:
            sample (Sample): The current sample with zones and parameters.

        Returns:
            Sample: The modified sample
        """
        likelihood = self.posterior_per_chain[sample.chain].likelihood
        lh_per_component = likelihood.update_component_likelihoods(sample=sample)
            # shape: (n_sites, n_features, 3)
        weights = likelihood.update_weights(sample=sample)
            # shape: (n_sites, n_features, 3)
        n_sites, n_features, k = weights.shape

        source_posterior = normalize(lh_per_component * weights, axis=-1)

        # sample the new source assignments
        eye = np.eye(3).astype(bool)
        sample.source = eye[sample_categorical(source_posterior)]

        assert np.min(np.sum(sample.source * weights, axis=-1)) > 0
        lh = likelihood.update_component_likelihoods(sample=sample)
        assert np.min(np.sum(lh * weights, axis=-1)) > 0

        # This is a Gibbs operator, which should always be accepted
        return sample, 0., 1.

    def gibbs_sample_weights(self, sample: Sample):
        observation_counts = np.sum(sample.source, axis=0)
        # TODO What to do with observations in languages that are not affected by any area/family

    def gibbs_sample_p_global(self, sample: Sample):
        # features shape:       (n_sites, n_features, n_states)
        # sample.source shape:  (n_sites, n_features, 3)
        assert sample.source.dtype == bool

        # Resample p_global for all features with ´k´ states.
        # Choose a random ´k´
        k = np.random.choice(np.unique(self.n_states_by_feature))

        # Select the relevant features
        features_with_k_states = (self.n_states_by_feature == k)
        features = self.features[:, features_with_k_states, :]

        # Only consider observations that are attributed to the global distribution
        from_global = sample.source[:, features_with_k_states, 0, np.newaxis]
        features = from_global * features

        # Resample p_global according to these observations
        # TODO Use prior counts for resampling (now this is just sampling from likelihood)
        for i, j in enumerate(np.argwhere(features_with_k_states)):
            sample.p_global[0, j, :] = np.random.dirichlet(1 + np.nansum(features[:, i], axis=0))
        # TODO find a way to do this vectorized
        # sample.p_global[0, features_with_k_states] = np.random.dirichlet(1 + np.nansum(features, axis=0))

        return sample, 0., 1.

    def gibbs_sample_p_zones(self, sample: Sample, zone=None):
        # features shape:       (n_sites, n_features, n_states)
        # sample.source shape:  (n_sites, n_features, 3)
        assert sample.source.dtype == bool

        if zone is None:
            zone = np.random.randint(0, self.n_zones)

        # Resample p_zones for all features with ´k´ states.
        # Choose a random ´k´
        k = np.random.choice(np.unique(self.n_states_by_feature))

        # Select the relevant features
        features_with_k_states = (self.n_states_by_feature == k)
        features = self.features[:, features_with_k_states, :]

        # Only consider observations that are attributed to the zone distribution
        from_zone = (sample.source[:, features_with_k_states, 1] & sample.zones[zone, :, np.newaxis])
        features = from_zone[...,np.newaxis] * features

        # Resample p_zones according to these observations
        # TODO Use prior counts for resampling (now this is just sampling from likelihood)
        for i, j in enumerate(np.argwhere(features_with_k_states)):
            sample.p_zones[zone, j, :] = np.random.dirichlet(1 + np.nansum(features[:, i, :], axis=0))
        # TODO find a way to do this vectorized
        # sample.p_zones[zone, features_with_k_states] = np.random.dirichlet(1 + np.nansum(features, axis=0))

        return sample, 0., 1.

    def gibbs_sample_p_families(self, sample: Sample, family=None):
        # features shape:       (n_sites, n_features, n_states)
        # sample.source shape:  (n_sites, n_features, 3)
        assert sample.source.dtype == bool

        if family is None:
            family = np.random.randint(0, self.n_families)

        # Resample p_families for all features with ´k´ states.
        # Choose a random ´k´
        k = np.random.choice(np.unique(self.n_states_by_feature))

        # Select the relevant features
        features_with_k_states = (self.n_states_by_feature == k)
        features = self.features[:, features_with_k_states, :]

        # Only consider observations that are attributed to the family distribution
        from_family = (sample.source[:, features_with_k_states, 2] & self.data.families[family, :, np.newaxis])
        features = from_family[...,np.newaxis] * features

        # Resample p_families according to these observations
        # TODO Use prior counts for resampling (now this is just sampling from likelihood)
        for i, j in enumerate(np.argwhere(features_with_k_states)):
            sample.p_families[family, j, :] = np.random.dirichlet(1 + np.nansum(features[:, i, :], axis=0))
        # TODO find a way to do this vectorized
        # sample.p_families[family, features_with_k_states] = np.random.dirichlet(1 + np.nansum(features, axis=0))

        return sample, 0., 1.

    def alter_weights(self, sample: Sample):
        """This function modifies one weight of one feature in the current sample

        Args:
            sample(Sample): The current sample with zones and parameters.
        Returns:
            Sample: The modified sample
        """
        sample_new = sample.copy()

        # Randomly choose one of the features
        f_id = np.random.choice(range(self.n_features))

        if self.inheritance:
            # Randomly choose two weights that will be changed, leave the others untouched
            weights_to_alter = _random.sample([0, 1, 2], 2)

            # Get the current weights
            weights_current = sample.weights[f_id, weights_to_alter]

            # Transform the weights such that they sum to 1
            weights_current_t = weights_current / weights_current.sum()

            # Propose new sample
            weights_new_t, q, q_back = self.dirichlet_proposal(weights_current_t, self.var_proposal_weight)

            # Transform back
            weights_new = weights_new_t * weights_current.sum()

            # Update
            sample_new.weights[f_id, weights_to_alter] = weights_new

        else:
            # if inheritance is not considered, there are only two weights.
            weights_current = sample.weights[f_id, :]
            weights_new, q, q_back = self.dirichlet_proposal(weights_current, self.var_proposal_weight)
            sample_new.weights[f_id, :] = weights_new

        # The step changed the weights (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['weights'] = True
        sample_new.what_changed['prior']['weights'] = True
        sample.what_changed['lh']['weights'] = True
        sample.what_changed['prior']['weights'] = True

        return sample_new, q, q_back

    def alter_p_global(self, sample):
        """This function modifies one p_global of one category and one feature in the current sample
            Args:
                 sample(Sample): The current sample with zones and parameters.
            Returns:
                 Sample: The modified sample
        """
        sample_new = sample.copy()

        # Randomly choose one of the features
        f_id = np.random.choice(range(self.n_features))

        # Different features have different applicable states
        f_states = np.nonzero(self.applicable_states[f_id])[0]

        # Randomly choose two applicable states for which the probabilities will be changed, leave the others untouched
        states_to_alter = _random.sample(list(f_states), 2)

        # Get the current probabilities
        p_current = sample.p_global[0, f_id, states_to_alter]

        # Transform the probabilities such that they sum to 1
        p_current_t = p_current / p_current.sum()

        # Propose new sample
        p_new_t, q, q_back = self.dirichlet_proposal(p_current_t, step_precision=self.var_proposal_p_global)

        # Transform back
        p_new = p_new_t * p_current.sum()

        # Update sample
        sample_new.p_global[0, f_id, states_to_alter] = p_new

        # The step changed p_global (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['p_global'].add(f_id)
        sample_new.what_changed['prior']['p_global'].add(f_id)
        sample.what_changed['lh']['p_global'].add(f_id)
        sample.what_changed['prior']['p_global'].add(f_id)

        return sample_new, q, q_back

    def alter_p_zones(self, sample):
        """This function modifies one p_zones of one category, one feature and in zone in the current sample
            Args:
                sample(Sample): The current sample with zones and parameters.
            Returns:
                Sample: The modified sample
                """
        sample_new = sample.copy()

        # Randomly choose one of the zones, one of the features and one of the categories
        z_id = np.random.choice(range(self.n_zones))
        f_id = np.random.choice(range(self.n_features))

        # Different features have different applicable states
        f_states = np.nonzero(self.applicable_states[f_id])[0]

        # Randomly choose two applicable states for which the probabilities will be changed, leave the others untouched
        states_to_alter = _random.sample(list(f_states), 2)

        # Get the current probabilities
        p_current = sample.p_zones[z_id, f_id, states_to_alter]

        # Transform the probabilities such that they sum to 1
        p_current_t = p_current / p_current.sum()

        # Sample new p from dirichlet distribution with given precision
        p_new_t, q, q_back = self.dirichlet_proposal(p_current_t, step_precision=self.var_proposal_p_zones)

        # Transform back
        p_new = p_new_t * p_current.sum()

        # Update sample
        sample_new.p_zones[z_id, f_id, states_to_alter] = p_new

        # The step changed p_zones (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['p_zones'].add((z_id, f_id))
        sample_new.what_changed['prior']['p_zones'].add((z_id, f_id))
        sample.what_changed['lh']['p_zones'].add((z_id, f_id))
        sample.what_changed['prior']['p_zones'].add((z_id, f_id))

        return sample_new, q, q_back

    @staticmethod
    def dirichlet_proposal(w, step_precision):
        """ A proposal distribution for normalized weight and probability vectors (summing to 1).

        Args:
            w (np.array): The weight vector, which is being resampled.
                Shape: (n_categories, )
            step_precision (float): The precision parameter controlling how narrow/wide the proposal
                distribution is. Low precision -> wide, high precision -> narrow.

        Returns:
            np.array: The newly proposed weights w_new (same shape as w).
            float: The transition probability q.
            float: The back probability q_back
        """
        # assert np.allclose(np.sum(w, axis=-1), 1.), w

        alpha = 1 + step_precision * w
        w_new = np.random.dirichlet(alpha)
        q = dirichlet_pdf(w_new, alpha)

        alpha_back = 1 + step_precision * w_new
        q_back = dirichlet_pdf(w, alpha_back)

        if not np.all(np.isfinite(w_new)):
            logging.warning(f'Dirichlet step resulted in NaN or Inf:')
            logging.warning(f'\tOld sample: {w}')
            logging.warning(f'\tstep_precision: {step_precision}')
            logging.warning(f'\tNew sample: {w_new}')
            # return w, 1., 0.

        return w_new, q, q_back

    def alter_p_families(self, sample):
        """This function modifies one p_families of one category, one feature and one family in the current sample
            Args:
                 sample(Sample): The current sample with zones and parameters.
            Returns:
                 Sample: The modified sample
        """

        sample_new = sample.copy()

        # Randomly choose one of the families and one of the features
        fam_id = np.random.choice(range(self.n_families))
        f_id = np.random.choice(range(self.n_features))

        # Different features have different applicable states
        f_states = np.nonzero(self.applicable_states[f_id])[0]

        # Randomly choose two applicable states for which the probabilities will be changed, leave the others untouched
        states_to_alter = _random.sample(list(f_states), 2)

        # Get the current probabilities
        p_current = sample.p_families[fam_id, f_id, states_to_alter]

        # Transform the probabilities such that they sum to 1
        p_current_t = p_current / p_current.sum()

        # Sample new p from dirichlet distribution with given precision
        p_new_t, q, q_back = self.dirichlet_proposal(p_current_t, step_precision=self.var_proposal_p_families)

        # Transform back
        p_new = p_new_t * p_current.sum()

        # Update sample
        sample_new.p_families[fam_id, f_id, states_to_alter] = p_new

        # The step changed p_families (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['p_families'].add((fam_id, f_id))
        sample_new.what_changed['prior']['p_families'].add((fam_id, f_id))
        sample.what_changed['lh']['p_families'].add((fam_id, f_id))
        sample.what_changed['prior']['p_families'].add((fam_id, f_id))

        return sample_new, q, q_back

    def swap_zone(self, sample):
        """ This functions swaps sites in one of the zones of the current sample
        (i.e. in of the zones a site is removed and another one added)
        Args:
            sample(Sample): The current sample with zones and weights.

        Returns:
            Sample: The modified sample.
         """
        sample_new = sample.copy()
        zones_current = sample.zones
        occupied = np.any(zones_current, axis=0)

        # Randomly choose one of the zones to modify
        z_id = np.random.choice(range(zones_current.shape[0]))
        zone_current = zones_current[z_id, :]

        neighbours = get_neighbours(zone_current, occupied, self.adj_mat)
        connected_step = (_random.random() < self.p_grow_connected)
        if connected_step:
            # All neighbors that are not yet occupied by other zones are candidates
            candidates = neighbours
        else:
            # All free sites are candidates
            candidates = ~occupied

        # When stuck (all neighbors occupied) return current sample and reject the step (q_back = 0)
        if not np.any(candidates):
            q, q_back = 1., 0.
            return sample, q, q_back

        # Add a site to the zone
        site_new = _random.choice(candidates.nonzero()[0])
        sample_new.zones[z_id, site_new] = 1

        # Remove a site from the zone
        removal_candidates = self.get_removal_candidates(zone_current)
        site_removed = _random.choice(removal_candidates)
        sample_new.zones[z_id, site_removed] = 0

        # # Compute transition probabilities
        back_neighbours = get_neighbours(zone_current, occupied, self.adj_mat)
        # q = 1. / np.count_nonzero(candidates)
        # q_back = 1. / np.count_nonzero(back_neighbours)

        # Transition probability growing to the new zone
        q_non_connected = 1 / np.count_nonzero(~occupied)
        q = (1 - self.p_grow_connected) * q_non_connected
        if neighbours[site_new]:
            q_connected = 1 / np.count_nonzero(neighbours)
            q += self.p_grow_connected * q_connected

        # Transition probability of growing back to the original zone
        q_back_non_connected = 1 / np.count_nonzero(~occupied)
        q_back = (1 - self.p_grow_connected) * q_back_non_connected
        # If z is a neighbour of the new zone, the back step could also be a connected grow step
        if back_neighbours[site_removed]:
            q_back_connected = 1 / np.count_nonzero(back_neighbours)
            q_back += self.p_grow_connected * q_back_connected

        if self.model.sample_source:
            sample_new, _, _ = self.gibbs_sample_sources(sample_new)

        # The step changed the zone (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['zones'].add(z_id)
        sample.what_changed['lh']['zones'].add(z_id)
        sample_new.what_changed['prior']['zones'].add(z_id)
        sample.what_changed['prior']['zones'].add(z_id)
        return sample_new, q, q_back

    def grow_zone(self, sample):
        """ This functions grows one of the zones in the current sample (i.e. it adds a new site to one of the zones)
        Args:
            sample(Sample): The current sample with zones and weights.

        Returns:
            (Sample): The modified sample.
        """

        sample_new = sample.copy()
        zones_current = sample.zones
        occupied = np.any(zones_current, axis=0)

        # Randomly choose one of the zones to modify
        z_id = np.random.choice(range(zones_current.shape[0]))
        zone_current = zones_current[z_id, :]

        # Check if zone is small enough to grow
        current_size = np.count_nonzero(zone_current)

        if current_size >= self.max_size:
            # Zone too big to grow: don't modify the sample and reject the step (q_back = 0)
            q, q_back = 1., 0.
            return sample, q, q_back

        neighbours = get_neighbours(zone_current, occupied, self.adj_mat)
        connected_step = (_random.random() < self.p_grow_connected)
        if connected_step:
            # All neighbors that are not yet occupied by other zones are candidates
            candidates = neighbours
        else:
            # All free sites are candidates
            candidates = ~occupied

        # When stuck (no candidates) return current sample and reject the step (q_back = 0)
        if not np.any(candidates):
            q, q_back = 1., 0.
            return sample, q, q_back

        # Choose a random candidate and add it to the zone
        site_new = _random.choice(candidates.nonzero()[0])
        sample_new.zones[z_id, site_new] = 1

        # Transition probability when growing
        q_non_connected = 1 / np.count_nonzero(~occupied)
        q = (1 - self.p_grow_connected) * q_non_connected

        if neighbours[site_new]:
            q_connected = 1 / np.count_nonzero(neighbours)
            q += self.p_grow_connected * q_connected

        # Back-probability (shrinking)
        q_back = 1 / (current_size + 1)

        if self.model.sample_source:
            sample_new, _, _ = self.gibbs_sample_sources(sample_new)

        # The step changed the zone (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['zones'].add(z_id)
        sample.what_changed['lh']['zones'].add(z_id)
        sample_new.what_changed['prior']['zones'].add(z_id)
        sample.what_changed['prior']['zones'].add(z_id)
        return sample_new, q, q_back

    def shrink_zone(self, sample):
        """ This functions shrinks one of the zones in the current sample (i.e. it removes one site from one zone)

        Args:
            sample(Sample): The current sample with zones and weights.

        Returns:
            (Sample): The modified sample.
        """
        sample_new = sample.copy()
        zones_current = sample.zones

        # Randomly choose one of the zones to modify
        z_id = np.random.choice(range(zones_current.shape[0]))
        zone_current = zones_current[z_id, :]

        # Check if zone is big enough to shrink
        current_size = np.count_nonzero(zone_current)
        if current_size <= self.min_size:
            # Zone is too small to shrink: don't modify the sample and reject the step (q_back = 0)
            q, q_back = 1., 0.
            return sample, q, q_back

        # Zone is big enough: shrink
        removal_candidates = self.get_removal_candidates(zone_current)
        site_removed = _random.choice(removal_candidates)
        sample_new.zones[z_id, site_removed] = 0

        # Transition probability when shrinking.
        q = 1 / len(removal_candidates)
        # Back-probability (growing)
        zone_new = sample_new.zones[z_id]
        occupied_new = np.any(sample_new.zones, axis=0)
        back_neighbours = get_neighbours(zone_new, occupied_new, self.adj_mat)

        # The back step could always be a non-connected grow step
        q_back_non_connected = 1 / np.count_nonzero(~occupied_new)
        q_back = (1 - self.p_grow_connected) * q_back_non_connected

        # If z is a neighbour of the new zone, the back step could also be a connected grow step
        if back_neighbours[site_removed]:
            q_back_connected = 1 / np.count_nonzero(back_neighbours)
            q_back += self.p_grow_connected * q_back_connected

        if self.model.sample_source:
            sample_new, _, _ = self.gibbs_sample_sources(sample_new)

        # The step changed the zone (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['zones'].add(z_id)
        sample.what_changed['lh']['zones'].add(z_id)
        sample_new.what_changed['prior']['zones'].add(z_id)
        sample.what_changed['prior']['zones'].add(z_id)
        return sample_new, q, q_back

    def generate_initial_zones(self):
        """For each chain (c) generate initial zones by
        A) growing through random grow-steps up to self.min_size,
        B) using the last sample of a previous run of the MCMC

        Returns:
            np.array: The generated initial zones.
                shape(n_zones, n_sites)
        """

        # If there are no zones, return empty matrix
        if self.n_zones == 0:
            return np.zeros((self.n_zones, self.n_sites), bool)

        occupied = np.zeros(self.n_sites, bool)
        initial_zones = np.zeros((self.n_zones, self.n_sites), bool)
        n_generated = 0

        # B: For those zones where a sample from a previous run exists we use this as the initial sample
        if self.initial_sample.zones is not None:
            for i in range(len(self.initial_sample.zones)):
                initial_zones[i, :] = self.initial_sample.zones[i]
                occupied += self.initial_sample.zones[i]
                n_generated += 1

        not_initialized = range(n_generated, self.n_zones)

        # A: The areas that are not initialized yet are grown
        # When there are already many areas, new ones can get stuck due to an unfavourable seed.
        # That's why we perform several attempts to initialize areas
        attempts = 0
        max_attempts = 1000

        while True:
            for i in not_initialized:
                try:
                    initial_size = self.initial_size
                    g = self.grow_zone_of_size_k(initial_size, occupied)

                except self.ZoneError:
                    # Might be due to an unfavourable seed

                    if attempts < max_attempts:
                        attempts += 1
                        not_initialized = range(n_generated, self.n_zones)
                        break
                    # Seems there is not enough sites to grow n_zones of size k
                    else:
                        raise ValueError("Failed to add additional area. Try fewer areas"
                                         "or set initial_sample to None")
                n_generated += 1
                initial_zones[i, :] = g[0]
                occupied = g[1]

            if n_generated == self.n_zones:
                return initial_zones

    def grow_zone_of_size_k(self, k, already_in_zone=None):
        """ This function grows a zone of size k excluding any of the sites in <already_in_zone>.
        Args:
            k (int): The size of the zone, i.e. the number of sites in the zone.
            already_in_zone (np.array): All sites already assigned to a zone (boolean)

        Returns:
            np.array: The newly grown zone (boolean).
            np.array: all nodes in the network already assigned to a zone (boolean).

        """
        if already_in_zone is None:
            already_in_zone = np.zeros(self.n_sites, bool)

        # Initialize the zone
        zone = np.zeros(self.n_sites, bool)

        # Find all sites that already belong to a zone (sites_occupied) and those that don't (sites_free)
        sites_occupied = np.nonzero(already_in_zone)[0]
        sites_free = set(range(self.n_sites)) - set(sites_occupied)

        # Take a random free site and use it as seed for the new zone
        try:
            i = _random.sample(sites_free, 1)[0]
            zone[i] = already_in_zone[i] = 1
        except ValueError:
            raise self.ZoneError

        # Grow the zone if possible
        for _ in range(k - 1):

            neighbours = get_neighbours(zone, already_in_zone, self.adj_mat)
            if not np.any(neighbours):
                raise self.ZoneError

            # Add a neighbour to the zone
            site_new = _random.choice(neighbours.nonzero()[0])
            zone[site_new] = already_in_zone[site_new] = 1

        return zone, already_in_zone

    def generate_initial_weights(self):
        """This function generates initial weights for the Bayesian additive mixture model, either by
        A) proposing them (randomly) from scratch
        B) using the last sample of a previous run of the MCMC
        Weights are in log-space and not normalized.

        Returns:
            np.array: weights for global, zone and family influence
            """

        # B: Use weights from a previous run
        if self.initial_sample.weights is not None:
            initial_weights = self.initial_sample.weights

        # A: Initialize new weights
        else:
            # When the algorithm does not include inheritance then there are only 2 weights (global and contact)
            if not self.inheritance:
                initial_weights = np.full((self.n_features, 2), 1.)

            else:
                initial_weights = np.full((self.n_features, 3), 1.)

        return normalize(initial_weights)

    def generate_initial_p_global(self):
        """This function generates initial global probabilities for each category either by
        A) using the MLE
        B) using the last sample of a previous run of the MCMC
        Probabilities are in log-space and not normalized.

        Returns:
            np.array: probabilities for categories in each family
                shape (1, n_features, max(n_categories))
        """
        initial_p_global = np.zeros((1, self.n_features, self.features.shape[2]))

        # B: Use p_global from a previous run
        if self.initial_sample.p_global is not None:
            initial_p_global = self.initial_sample.p_global

        # A: Initialize new p_global using the MLE
        else:

            sites_per_state = np.count_nonzero(self.features, axis=0)
            # Some areas have nan for all states, resulting in a non-defined MLE
            # other areas have only a single state, resulting in an MLE including 1.
            # to avoid both, we add 1 to all applicable states of each feature,
            # which gives a well-defined initial p_zone without 1., slightly nudged away from the MLE

            sites_per_state[np.isnan(sites_per_state)] = 0
            sites_per_state[self.applicable_states] += 1
            site_sums = np.sum(sites_per_state, axis=1)
            p_global = sites_per_state / site_sums[:, np.newaxis]

            initial_p_global[0, :, :] = p_global
        return initial_p_global

    def set_p_zones_to_mle(self, updated_zone):
        """This function sets the p_zones to the MLE of the current zone
        Probabilities are in log-space and not normalized.
        Args:
            updated_zone (np.array): The currently updated zone
            (n_sites)
        Returns:
            np.array: probabilities for categories in each zones
                shape (n_zones, n_features, max(n_categories))
        """

        idx = updated_zone.nonzero()[0]
        features_zone = self.features[idx, :, :]
        l_per_cat = np.sum(features_zone, axis=0)
        p_zones = normalize(l_per_cat)

        return p_zones

    def generate_initial_p_zones(self, initial_zones):
        """This function generates initial probabilities for categories in each of the zones, either by
        A) proposing them (randomly) from scratch
        B) using the last sample of a previous run of the MCMC
        Probabilities are in log-space and not normalized.
        Args:
            initial_zones: The assignment of sites to zones
            (n_zones, n_sites)
        Returns:
            np.array: probabilities for categories in each zones
                shape (n_zones, n_features, max(n_categories))
        """
        # For convenience all p_zones go in one array, even though not all features have the same number of categories
        initial_p_zones = np.zeros((self.n_zones, self.n_features, self.features.shape[2]))
        n_generated = 0

        # B: Use p_zones from a previous run
        if self.initial_sample.p_zones is not None:

            for i in range(len(self.initial_sample.p_zones)):
                initial_p_zones[i, :] = self.initial_sample.p_zones[i]
                n_generated += 1

        not_initialized = range(n_generated, self.n_zones)

        # A: Initialize new p_zones using a value close to the MLE of the current zone
        for i in not_initialized:
            idx = initial_zones[i].nonzero()[0]
            features_zone = self.features[idx, :, :]

            sites_per_state = np.nansum(features_zone, axis=0)

            # Some areas have nan for all states, resulting in a non-defined MLE
            # other areas have only a single state, resulting in an MLE including 1.
            # to avoid both, we add 1 to all applicable states of each feature,
            # which gives a well-defined initial p_zone without 1., slightly nudged away from the MLE

            sites_per_state[np.isnan(sites_per_state)] = 0
            sites_per_state[self.applicable_states] += 1

            site_sums = np.sum(sites_per_state, axis=1)
            p_zones = sites_per_state / site_sums[:, np.newaxis]

            initial_p_zones[i, :, :] = p_zones

        return initial_p_zones

    def generate_initial_p_families(self):
        """This function generates initial probabilities for categories in each of the families, either by
        A) using the MLE
        B) using the last sample of a previous run of the MCMC
        Probabilities are in log-space and not normalized.

        Returns:
            np.array: probabilities for categories in each family
                shape (n_families, n_features, max(n_categories))
        """
        initial_p_families = np.zeros((self.n_families, self.n_features, self.features.shape[2]))

        # B: Use p_families from a previous run
        if self.initial_sample.p_families is not None:
            for i in range(len(self.initial_sample.p_families)):
                initial_p_families[i, :] = self.initial_sample.p_families[i]

        # A: Initialize new p_families using the MLE
        else:

            for fam in range(len(self.families)):
                idx = self.families[fam].nonzero()[0]
                features_family = self.features[idx, :, :]

                sites_per_state = np.nansum(features_family, axis=0)

                # Compute the MLE for each category and each family
                # Some families have only NAs for some features, resulting in a non-defined MLE
                # other families have only a single state, resulting in an MLE including 1.
                # to avoid both, we add 1 to all applicable states of each feature,
                # which gives a well-defined initial p_family without 1., slightly nudged away from the MLE

                sites_per_state[np.isnan(sites_per_state)] = 0
                sites_per_state[self.applicable_states] += 1

                state_sums = np.sum(sites_per_state, axis=1)
                p_family = sites_per_state / state_sums[:, np.newaxis]
                initial_p_families[fam, :, :] = p_family

        return initial_p_families

    def generate_initial_sample(self, c=0):
        """Generate initial Sample object (zones, weights)
        Kwargs:
            c (int): index of the MC3 chain.
        Returns:
            Sample: The generated initial Sample
        """
        # Zones
        initial_zones = self.generate_initial_zones()

        # Weights
        initial_weights = self.generate_initial_weights()

        # p_global (alpha)

        initial_p_global = self.generate_initial_p_global()

        # p_zones (gamma)
        initial_p_zones = self.generate_initial_p_zones(initial_zones)

        # p_families (beta)
        if self.inheritance:
            initial_p_families = self.generate_initial_p_families()
        else:
            initial_p_families = None

        sample = Sample(zones=initial_zones, weights=initial_weights,
                        p_global=initial_p_global, p_zones=initial_p_zones, p_families=initial_p_families,
                        chain=c)

        # Generate the initial source using a Gibbs sampling step
        if self.model.sample_source:
            sample, _, _ = self.gibbs_sample_sources(sample)

        return sample

    @staticmethod
    def get_removal_candidates(zone):
        """Finds sites which can be removed from the given zone.

        Args:
            zone (np.array): The zone for which removal candidates are found.
                shape(n_sites)
        Returns:
            (list): Index-list of removal candidates.
        """
        return zone.nonzero()[0]

    class ZoneError(Exception):
        pass

    def get_operators(self, operators):
        """Get all relevant operator functions for proposing MCMC update steps and their probabilities

        Args:
            operators(dict): dictionary with names of all operators (keys) and their weights (values)

        Returns:
            list, list: the operator functions (callable), their weights (float)
        """
        fn_operators = []
        p_operators = []

        for k, v in operators.items():
            fn_operators.append(getattr(self, k))
            p_operators.append(v)

        return fn_operators, p_operators

    def log_sample_statistics(self, sample, c, sample_id):
        super(ZoneMCMC, self).log_sample_statistics(sample, c, sample_id)


class ZoneMCMCWarmup(ZoneMCMC):

    IS_WARMUP = True

    def __init__(self, **kwargs):
        super(ZoneMCMCWarmup, self).__init__(**kwargs)

        # In warmup chains can have a different max_size for areas
        self.max_size = get_max_size_list(
            start=(self.initial_size + self.max_size)/4,
            end=self.max_size,
            n_total=self.n_chains,
            k_groups=4
        )

        # Some chains only have connected steps, whereas others also have random steps
        self.p_grow_connected = _random.choices(
            population=[1, self.p_grow_connected],
            k=self.n_chains
        )

    def gibbs_sample_sources(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).gibbs_sample_sources(sample)

    def gibbs_sample_p_global(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).gibbs_sample_p_global(sample)

    def gibbs_sample_p_zones(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).gibbs_sample_p_zones(sample)

    def gibbs_sample_p_families(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).gibbs_sample_p_families(sample)

    def alter_weights(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).alter_weights(sample)

    def alter_p_global(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).alter_p_global(sample)

    def alter_p_zones(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).alter_p_zones(sample)

    def swap_zone(self, sample, c=0):
        """ This functions swaps sites in one of the zones of the current sample
        (i.e. in of the zones a site is removed and another one added)
        Args:
            sample(Sample): The current sample with zones and weights.
            c(int): The current warmup chain
        Returns:
            Sample: The modified sample.
         """
        sample_new = sample.copy()
        zones_current = sample.zones
        occupied = np.any(zones_current, axis=0)

        # Randomly choose one of the zones to modify
        z_id = np.random.choice(range(zones_current.shape[0]))
        zone_current = zones_current[z_id, :]

        neighbours = get_neighbours(zone_current, occupied, self.adj_mat)
        connected_step = (_random.random() < self.p_grow_connected[c])
        if connected_step:
            # All neighbors that are not yet occupied by other zones are candidates
            candidates = neighbours
        else:
            # All free sites are candidates
            candidates = ~occupied

        # When stuck (all neighbors occupied) return current sample and reject the step (q_back = 0)
        if not np.any(candidates):
            q, q_back = 1., 0.
            return sample, q, q_back

        # Add a site to the zone
        site_new = _random.choice(candidates.nonzero()[0])
        sample_new.zones[z_id, site_new] = 1

        # Remove a site from the zone
        removal_candidates = self.get_removal_candidates(zone_current)
        site_removed = _random.choice(removal_candidates)
        sample_new.zones[z_id, site_removed] = 0

        # # Compute transition probabilities
        back_neighbours = get_neighbours(zone_current, occupied, self.adj_mat)
        # q = 1. / np.count_nonzero(candidates)
        # q_back = 1. / np.count_nonzero(back_neighbours)

        # Transition probability growing to the new zone
        q_non_connected = 1 / np.count_nonzero(~occupied)

        q = (1 - self.p_grow_connected[c]) * q_non_connected
        if neighbours[site_new]:
            q_connected = 1 / np.count_nonzero(neighbours)
            q += self.p_grow_connected[c] * q_connected

        # Transition probability of growing back to the original zone
        q_back_non_connected = 1 / np.count_nonzero(~occupied)
        q_back = (1 - self.p_grow_connected[c]) * q_back_non_connected
        # If z is a neighbour of the new zone, the back step could also be a connected grow step
        if back_neighbours[site_removed]:
            q_back_connected = 1 / np.count_nonzero(back_neighbours)
            q_back += self.p_grow_connected[c] * q_back_connected

        # The step changed the zone (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['zones'].add(z_id)
        sample.what_changed['lh']['zones'].add(z_id)
        sample_new.what_changed['prior']['zones'].add(z_id)
        sample.what_changed['prior']['zones'].add(z_id)

        return sample_new, q, q_back

    def grow_zone(self, sample, c=0):
        """ This functions grows one of the zones in the current sample (i.e. it adds a new site to one of the zones)
        Args:
            sample(Sample): The current sample with zones and weights.
            c(int): The current warmup chain
        Returns:
            (Sample): The modified sample.
        """
        sample_new = sample.copy()
        zones_current = sample.zones
        occupied = np.any(zones_current, axis=0)

        # Randomly choose one of the zones to modify
        z_id = np.random.choice(range(zones_current.shape[0]))
        zone_current = zones_current[z_id, :]

        # Check if zone is small enough to grow
        current_size = np.count_nonzero(zone_current)

        if current_size >= self.max_size[c]:
            # Zone too big to grow: don't modify the sample and reject the step (q_back = 0)
            q, q_back = 1., 0.
            return sample, q, q_back

        neighbours = get_neighbours(zone_current, occupied, self.adj_mat)
        connected_step = (_random.random() < self.p_grow_connected[c])
        if connected_step:
            # All neighbors that are not yet occupied by other zones are candidates
            candidates = neighbours
        else:
            # All free sites are candidates
            candidates = ~occupied

        # When stuck (no candidates) return current sample and reject the step (q_back = 0)
        if not np.any(candidates):
            q, q_back = 1., 0.
            return sample, q, q_back

        # Choose a random candidate and add it to the zone
        site_new = _random.choice(candidates.nonzero()[0])
        sample_new.zones[z_id, site_new] = 1

        # Transition probability when growing
        q_non_connected = 1 / np.count_nonzero(~occupied)
        q = (1 - self.p_grow_connected[c]) * q_non_connected

        if neighbours[site_new]:
            q_connected = 1 / np.count_nonzero(neighbours)
            q += self.p_grow_connected[c] * q_connected

        # Back-probability (shrinking)
        q_back = 1 / (current_size + 1)

        # The step changed the zone (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['zones'].add(z_id)
        sample.what_changed['lh']['zones'].add(z_id)
        sample_new.what_changed['prior']['zones'].add(z_id)
        sample.what_changed['prior']['zones'].add(z_id)

        return sample_new, q, q_back

    def shrink_zone(self, sample, c=0):
        """ This functions shrinks one of the zones in the current sample (i.e. it removes one site from one zone)

        Args:
            sample(Sample): The current sample with zones and weights.
            c(int): The current warmup chain
        Returns:
            (Sample): The modified sample.
        """
        sample_new = sample.copy()
        zones_current = sample.zones

        # Randomly choose one of the zones to modify
        z_id = np.random.choice(range(zones_current.shape[0]))
        zone_current = zones_current[z_id, :]

        # Check if zone is big enough to shrink
        current_size = np.count_nonzero(zone_current)
        if current_size <= self.min_size:
            # Zone is too small to shrink: don't modify the sample and reject the step (q_back = 0)
            q, q_back = 1., 0.
            return sample, q, q_back

        # Zone is big enough: shrink
        removal_candidates = self.get_removal_candidates(zone_current)
        site_removed = _random.choice(removal_candidates)
        sample_new.zones[z_id, site_removed] = 0

        # Transition probability when shrinking.
        q = 1 / len(removal_candidates)
        # Back-probability (growing)
        zone_new = sample_new.zones[z_id]
        occupied_new = np.any(sample_new.zones, axis=0)
        back_neighbours = get_neighbours(zone_new, occupied_new, self.adj_mat)

        # The back step could always be a non-connected grow step
        q_back_non_connected = 1 / np.count_nonzero(~occupied_new)
        q_back = (1 - self.p_grow_connected[c]) * q_back_non_connected

        # If z is a neighbour of the new zone, the back step could also be a connected grow step
        if back_neighbours[site_removed]:
            q_back_connected = 1 / np.count_nonzero(back_neighbours)
            q_back += self.p_grow_connected[c] * q_back_connected

        # Back-probability (shrinking)
        q_back = 1 / (current_size + 1)

        # self.q_areas_stats['q_shrink'].append(q)
        # self.q_areas_stats['q_back_shrink'].append(q_back)

        # The step changed the zone (which has an influence on how the lh and the prior look like)
        sample_new.what_changed['lh']['zones'].add(z_id)
        sample.what_changed['lh']['zones'].add(z_id)
        sample_new.what_changed['prior']['zones'].add(z_id)
        sample.what_changed['prior']['zones'].add(z_id)
        return sample_new, q, q_back

    def alter_p_families(self, sample, c=0):
        return super(ZoneMCMCWarmup, self).alter_p_families(sample)
