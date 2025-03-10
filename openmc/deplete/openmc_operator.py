"""OpenMC transport operator

This module implements functions shared by both OpenMC transport-coupled and
transport-independent transport operators.

"""

from abc import abstractmethod
from collections import OrderedDict

import numpy as np

import openmc
from openmc.exceptions import DataError
from openmc.mpi import comm
from .abc import TransportOperator, OperatorResult
from .atom_number import AtomNumber
from .reaction_rates import ReactionRates

__all__ = ["OpenMCOperator", "OperatorResult"]


def _distribute(items):
    """Distribute items across MPI communicator

    Parameters
    ----------
    items : list
        List of items of distribute

    Returns
    -------
    list
        Items assigned to process that called

    """
    min_size, extra = divmod(len(items), comm.size)
    j = 0
    for i in range(comm.size):
        chunk_size = min_size + int(i < extra)
        if comm.rank == i:
            return items[j:j + chunk_size]
        j += chunk_size


class OpenMCOperator(TransportOperator):
    """Abstract class holding OpenMC-specific functions for running
    depletion calculations.

    Specific classes for running transport-coupled or transport-independent
    depletion calculations are implemented as subclasses of OpenMCOperator.

    Parameters
    ----------
    materials : openmc.Materials
        List of all materials in the model
    cross_sections : str or pandas.DataFrame
        Path to continuous energy cross section library, or object containing
        one-group cross-sections.
    chain_file : str, optional
        Path to the depletion chain XML file. Defaults to
        openmc.config['chain_file'].
    prev_results : Results, optional
        Results from a previous depletion calculation. If this argument is
        specified, the depletion calculation will start from the latest state
        in the previous results.
    diff_burnable_mats : bool, optional
        Whether to differentiate burnable materials with multiple instances.
        Volumes are divided equally from the original material volume.
    fission_q : dict, optional
        Dictionary of nuclides and their fission Q values [eV].
    dilute_initial : float, optional
        Initial atom density [atoms/cm^3] to add for nuclides that are zero
        in initial condition to ensure they exist in the decay chain.
        Only done for nuclides with reaction rates.
    helper_kwargs : dict
        Keyword arguments for helper classes
    reduce_chain : bool, optional
        If True, use :meth:`openmc.deplete.Chain.reduce()` to reduce the
        depletion chain up to ``reduce_chain_level``.
    reduce_chain_level : int, optional
        Depth of the search when reducing the depletion chain. Only used
        if ``reduce_chain`` evaluates to true. The default value of
        ``None`` implies no limit on the depth.

    Attributes
    ----------
    materials : openmc.Materials
        All materials present in the model
    cross_sections : str or MicroXS
            Path to continuous energy cross section library, or object
            containing one-group cross-sections.
    dilute_initial : float
        Initial atom density [atoms/cm^3] to add for nuclides that
        are zero in initial condition to ensure they exist in the decay
        chain. Only done for nuclides with reaction rates.
    output_dir : pathlib.Path
        Path to output directory to save results.
    round_number : bool
        Whether or not to round output to OpenMC to 8 digits.
        Useful in testing, as OpenMC is incredibly sensitive to exact values.
    number : openmc.deplete.AtomNumber
        Total number of atoms in simulation.
    nuclides_with_data : set of str
        A set listing all unique nuclides available from cross_sections.xml.
    chain : openmc.deplete.Chain
        The depletion chain information necessary to form matrices and tallies.
    reaction_rates : openmc.deplete.ReactionRates
        Reaction rates from the last operator step.
    burnable_mats : list of str
        All burnable material IDs
    heavy_metal : float
        Initial heavy metal inventory [g]
    local_mats : list of str
        All burnable material IDs being managed by a single process
    prev_res : Results or None
        Results from a previous depletion calculation. ``None`` if no
        results are to be used.

    """

    def __init__(
            self,
            materials=None,
            cross_sections=None,
            chain_file=None,
            prev_results=None,
            diff_burnable_mats=False,
            fission_q=None,
            dilute_initial=0.0,
            helper_kwargs=None,
            reduce_chain=False,
            reduce_chain_level=None):

        # If chain file was not specified, try to get it from global config
        if chain_file is None:
            chain_file = openmc.config.get('chain_file')
            if chain_file is None:
                raise DataError(
                    "No depletion chain specified and could not find depletion "
                    "chain in openmc.config['chain_file']"
                )

        super().__init__(chain_file, fission_q, dilute_initial, prev_results)
        self.round_number = False
        self.materials = materials
        self.cross_sections = cross_sections

        # Reduce the chain to only those nuclides present
        if reduce_chain:
            init_nuclides = set()
            for material in self.materials:
                if not material.depletable:
                    continue
                for name, _dens_percent, _dens_type in material.nuclides:
                    init_nuclides.add(name)

            self.chain = self.chain.reduce(init_nuclides, reduce_chain_level)

        if diff_burnable_mats:
            self._differentiate_burnable_mats()

        # Determine which nuclides have cross section data
        # This nuclides variables contains every nuclides
        # for which there is an entry in the micro_xs parameter
        openmc.reset_auto_ids()
        self.burnable_mats, volumes, all_nuclides = self._get_burnable_mats()
        self.local_mats = _distribute(self.burnable_mats)

        self._mat_index_map = {
            lm: self.burnable_mats.index(lm) for lm in self.local_mats}

        if self.prev_res is not None:
            self._load_previous_results()

        self.nuclides_with_data = self._get_nuclides_with_data(
            self.cross_sections)

        # Select nuclides with data that are also in the chain
        self._burnable_nucs = [nuc.name for nuc in self.chain.nuclides
                               if nuc.name in self.nuclides_with_data]

        # Extract number densities from the geometry / previous depletion run
        self._extract_number(self.local_mats,
                             volumes,
                             all_nuclides,
                             self.prev_res)

        # Create reaction rates array
        self.reaction_rates = ReactionRates(
            self.local_mats, self._burnable_nucs, self.chain.reactions)

        self._get_helper_classes(helper_kwargs)

    def _differentiate_burnable_mats(self):
        """Assign distribmats for each burnable material"""
        pass

    def _get_burnable_mats(self):
        """Determine depletable materials, volumes, and nuclides

        Returns
        -------
        burnable_mats : list of str
            List of burnable material IDs
        volume : OrderedDict of str to float
            Volume of each material in [cm^3]
        nuclides : list of str
            Nuclides in order of how they'll appear in the simulation.

        """

        burnable_mats = set()
        model_nuclides = set()
        volume = OrderedDict()

        self.heavy_metal = 0.0

        # Iterate once through the geometry to get dictionaries
        for mat in self.materials:
            for nuclide in mat.get_nuclides():
                model_nuclides.add(nuclide)
            if mat.depletable:
                burnable_mats.add(str(mat.id))
                if mat.volume is None:
                    raise RuntimeError("Volume not specified for depletable "
                                       "material with ID={}.".format(mat.id))
                volume[str(mat.id)] = mat.volume
                self.heavy_metal += mat.fissionable_mass

        # Make sure there are burnable materials
        if not burnable_mats:
            raise RuntimeError(
                "No depletable materials were found in the model.")

        # Sort the sets
        burnable_mats = sorted(burnable_mats, key=int)
        model_nuclides = sorted(model_nuclides)

        # Construct a global nuclide dictionary, burned first
        nuclides = list(self.chain.nuclide_dict)
        for nuc in model_nuclides:
            if nuc not in nuclides:
                nuclides.append(nuc)

        return burnable_mats, volume, nuclides

    def _load_previous_results(self):
        """Load results from a previous depletion simulation"""
        pass

    @abstractmethod
    def _get_nuclides_with_data(self, cross_sections):
        """Find nuclides with cross section data

        Parameters
        ----------
        cross_sections : str or pandas.DataFrame
            Path to continuous energy cross section library, or object
            containing one-group cross-sections.

        Returns
        -------
        nuclides : set of str
            Set of nuclide names that have cross secton data

        """

    def _extract_number(self, local_mats, volume, all_nuclides, prev_res=None):
        """Construct AtomNumber using geometry

        Parameters
        ----------
        local_mats : list of str
            Material IDs to be managed by this process
        volume : OrderedDict of str to float
            Volumes for the above materials in [cm^3]
        all_nuclides : list of str
            Nuclides to be used in the simulation.
        prev_res : Results, optional
            Results from a previous depletion calculation

        """
        self.number = AtomNumber(local_mats, all_nuclides, volume, len(self.chain))

        if self.dilute_initial != 0.0:
            for nuc in self._burnable_nucs:
                self.number.set_atom_density(
                    np.s_[:], nuc, self.dilute_initial)

        # Now extract and store the number densities
        # From the geometry if no previous depletion results
        if prev_res is None:
            for mat in self.materials:
                if str(mat.id) in local_mats:
                    self._set_number_from_mat(mat)

        # Else from previous depletion results
        else:
            for mat in self.materials:
                if str(mat.id) in local_mats:
                    self._set_number_from_results(mat, prev_res)

    def _set_number_from_mat(self, mat):
        """Extracts material and number densities from openmc.Material

        Parameters
        ----------
        mat : openmc.Material
            The material to read from

        """
        mat_id = str(mat.id)

        for nuclide, atom_per_bcm in mat.get_nuclide_atom_densities().items():
            atom_per_cc = atom_per_bcm * 1.0e24
            self.number.set_atom_density(mat_id, nuclide, atom_per_cc)

    def _set_number_from_results(self, mat, prev_res):
        """Extracts material nuclides and number densities.

        If the nuclide concentration's evolution is tracked, the densities come
        from depletion results. Else, densities are extracted from the geometry
        in the summary.

        Parameters
        ----------
        mat : openmc.Material
            The material to read from
        prev_res : Results
            Results from a previous depletion calculation

        """
        mat_id = str(mat.id)

        # Get nuclide lists from geometry and depletion results
        depl_nuc = prev_res[-1].index_nuc
        geom_nuc_densities = mat.get_nuclide_atom_densities()

        # Merge lists of nuclides, with the same order for every calculation
        geom_nuc_densities.update(depl_nuc)

        for nuclide, atom_per_bcm in geom_nuc_densities.items():
            if nuclide in depl_nuc:
                concentration = prev_res.get_atoms(mat_id, nuclide)[1][-1]
                volume = prev_res[-1].volume[mat_id]
                atom_per_cc = concentration / volume
            else:
                atom_per_cc = atom_per_bcm * 1.0e24

            self.number.set_atom_density(mat_id, nuclide, atom_per_cc)

    @abstractmethod
    def _get_helper_classes(self, helper_kwargs):
        """Create the ``_rate_helper``, ``_normalization_helper``, and
        ``_yield_helper`` objects.

        Parameters
        ----------
        helper_kwargs : dict
            Keyword arguments for helper classes

        """

    def initial_condition(self, materials):
        """Performs final setup and returns initial condition.

        Parameters
        ----------
        materials : list of str
            list of material IDs

        Returns
        -------
        list of numpy.ndarray
            Total density for initial conditions.

        """

        self._rate_helper.generate_tallies(materials, self.chain.reactions)
        self._normalization_helper.prepare(
            self.chain.nuclides, self.reaction_rates.index_nuc)
        # Tell fission yield helper what materials this process is
        # responsible for
        self._yield_helper.generate_tallies(
            materials, tuple(sorted(self._mat_index_map.values())))

        # Return number density vector
        return list(self.number.get_mat_slice(np.s_[:]))

    def _update_materials_and_nuclides(self, vec):
        """Update the number density, material compositions, and nuclide
        lists in helper objects

        Parameters
        ----------
        vec : list of numpy.ndarray
            Total atoms.

        """

        # Update the number densities regardless of the source rate
        self.number.set_density(vec)
        self._update_materials()

        # Prevent OpenMC from complaining about re-creating tallies
        openmc.reset_auto_ids()

        # Update tally nuclides data in preparation for transport solve
        nuclides = self._get_reaction_nuclides()
        self._rate_helper.nuclides = nuclides
        self._normalization_helper.nuclides = nuclides
        self._yield_helper.update_tally_nuclides(nuclides)

    @abstractmethod
    def _update_materials(self):
        """Updates material compositions in OpenMC on all processes."""

    def write_bos_data(self, step):
        """Document beginning of step data for a given step

        Called at the beginning of a depletion step and at
        the final point in the simulation.

        Parameters
        ----------
        step : int
            Current depletion step including restarts
        """
        # Since we aren't running a transport simulation, we simply pass
        pass

    def _get_reaction_nuclides(self):
        """Determine nuclides that should be tallied for reaction rates.

        This method returns a list of all nuclides that have cross section data
        and are listed in the depletion chain. Technically, we should count
        nuclides that may not appear in the depletion chain because we still
        need to get the fission reaction rate for these nuclides in order to
        normalize power, but that is left as a future exercise.

        Returns
        -------
        list of str
            Nuclides with reaction rates

        """
        nuc_set = set()

        # Create the set of all nuclides in the decay chain in materials marked
        # for burning in which the number density is greater than zero.
        for nuc in self.number.nuclides:
            if nuc in self.nuclides_with_data:
                if np.sum(self.number[:, nuc]) > 0.0:
                    nuc_set.add(nuc)

        # Communicate which nuclides have nonzeros to rank 0
        if comm.rank == 0:
            for i in range(1, comm.size):
                nuc_newset = comm.recv(source=i, tag=i)
                nuc_set |= nuc_newset
        else:
            comm.send(nuc_set, dest=0, tag=comm.rank)

        if comm.rank == 0:
            # Sort nuclides in the same order as self.number
            nuc_list = [nuc for nuc in self.number.nuclides
                        if nuc in nuc_set]
        else:
            nuc_list = None

        # Store list of nuclides on each process
        nuc_list = comm.bcast(nuc_list)
        return [nuc for nuc in nuc_list if nuc in self.chain]

    def _calculate_reaction_rates(self, source_rate):
        """Unpack tallies from OpenMC and return an operator result

        This method uses OpenMC's C API bindings to determine the k-effective
        value and reaction rates from the simulation. The reaction rates are
        normalized by a helper class depending on the method being used.

        Parameters
        ----------
        source_rate : float
            Power in [W] or source rate in [neutron/sec]

        Returns
        -------
        rates : openmc.deplete.ReactionRates
            Reaction rates for nuclides

        """
        rates = self.reaction_rates
        rates.fill(0.0)

        # Extract reaction nuclides
        rxn_nuclides = self._rate_helper.nuclides

        # Form fast map
        nuc_ind = [rates.index_nuc[nuc] for nuc in rxn_nuclides]
        react_ind = [rates.index_rx[react] for react in self.chain.reactions]

        # Keep track of energy produced from all reactions in eV per source
        # particle
        self._normalization_helper.reset()
        self._yield_helper.unpack()

        # Store fission yield dictionaries
        fission_yields = []

        # Create arrays to store fission Q values, reaction rates, and nuclide
        # numbers, zeroed out in material iteration
        number = np.empty(rates.n_nuc)

        fission_ind = rates.index_rx.get("fission")

        # Reset the cached material reaction rates tallies
        self._rate_helper.reset_tally_means()

        # Extract results
        for i, mat in enumerate(self.local_mats):
            # Get tally index
            mat_index = self._mat_index_map[mat]

            # Zero out reaction rates and nuclide numbers
            number.fill(0.0)

            # Get new number densities
            for nuc, i_nuc_results in zip(rxn_nuclides, nuc_ind):
                number[i_nuc_results] = self.number[mat, nuc]

            tally_rates = self._rate_helper.get_material_rates(
                mat_index, nuc_ind, react_ind)

            # Compute fission yields for this material
            fission_yields.append(self._yield_helper.weighted_yields(i))

            # Accumulate energy from fission
            if fission_ind is not None:
                self._normalization_helper.update(
                    tally_rates[:, fission_ind])

            # Divide by total number of atoms and store
            rates[i] = self._rate_helper.divide_by_atoms(number)

        # Scale reaction rates to obtain units of (reactions/sec)/atom
        rates *= self._normalization_helper.factor(source_rate)

        # Store new fission yields on the chain
        self.chain.fission_yields = fission_yields

        return rates

    def get_results_info(self):
        """Returns volume list, material lists, and nuc lists.

        Returns
        -------
        volume : dict of str float
            Volumes corresponding to materials in full_burn_dict
        nuc_list : list of str
            A list of all nuclide names. Used for sorting the simulation.
        burn_list : list of int
            A list of all material IDs to be burned.  Used for sorting the simulation.
        full_burn_list : list
            List of all burnable material IDs

        """
        nuc_list = self.number.burnable_nuclides
        burn_list = self.local_mats

        volume = {}
        for i, mat in enumerate(burn_list):
            volume[mat] = self.number.volume[i]

        # Combine volume dictionaries across processes
        volume_list = comm.allgather(volume)
        volume = {k: v for d in volume_list for k, v in d.items()}

        return volume, nuc_list, burn_list, self.burnable_mats
