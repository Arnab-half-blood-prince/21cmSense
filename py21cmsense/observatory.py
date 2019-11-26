"""
Module providing the definition of an Observatory.

This replaces the original usage of an aipy.AntennaArray with something much more
simple, and suited to the needs of this particular package.
"""

import collections
from collections import defaultdict

import attr
import numpy as np
import tqdm
import yaml
from astropy import constants as cnst
from astropy import units as units
from attr import validators as vld
from cached_property import cached_property

from . import _utils as ut
from . import antpos as antpos_module
from . import beam, config


@attr.s(frozen=True, kw_only=True, cmp=False)
class Observatory:
    """
    A class defining an interferometric Observatory and its properties.

    Parameters
    ----------
    antpos : array
        An array with shape (Nants, 3) specifying the positions of the antennas.
        These should be in the ENU (East-North-Up) frame, relative to a central location
        given by `latitude`. If not a Quantity, units are assumed to be meters.
    beam : :class:`~py21cmsense.beam.PrimaryBeam` instance
        A beam, assumed to be homogeneous across antennas.
    latitude : float or Quantity, optional
        Latitude of the array center. If a float, assumed to be in radians.
        Note that longitude is not required, as we assume an isotropic sky.
    Trcv : float or Quantity
        Receiver temperature, assumed to be in mK unless otherwise defined.
    """

    antpos = attr.ib(converter=ut.apply_or_convert_unit("m"))
    beam = attr.ib(validator=vld.instance_of(beam.PrimaryBeam))
    latitude = attr.ib(
        0,
        converter=ut.apply_or_convert_unit("rad"),
        validator=ut.between(-np.pi * units.rad / 2, np.pi * units.rad / 2),
    )
    Trcv = attr.ib(
        1e5, converter=ut.apply_or_convert_unit("mK"), validator=ut.nonnegative
    )

    @antpos.validator
    def _antpos_validator(self, att, val):
        assert val.ndim == 2
        assert val.shape[-1] == 3
        assert val.shape[0] > 1

    @property
    def frequency(self):
        """Central frequency of the observation"""
        return self.beam.frequency

    @cached_property
    def n_antennas(self):
        """Number of antennas in the array"""
        return len(self.antpos)

    def clone(self, **kwargs):
        """Return a clone of this instance, but change kwargs"""
        return attr.evolve(self, **kwargs)

    @classmethod
    def from_uvdata(cls, uvdata, beam):
        """Instantiate an Observatory from a pyuvdata.UVData object or compatible file"""
        try:
            import pyuvdata
        except ImportError:
            raise ImportError(
                "cannot construct Observatory from uvdata object without "
                "pyuvdata being installed!"
            )

        if isinstance(uvdata, str):
            uv = pyuvdata.UVData()
            uv.read(uvdata)
        else:
            uv = uvdata

        return cls(
            antpos=uv.antenna_positions, beam=beam, latitude=uv.telescope_lat_lon_alt[0]
        )

    @classmethod
    def from_yaml(cls, yaml_file):
        """Instantiate an Observatory from a compatible YAML config file."""
        if isinstance(yaml_file, str):
            with open(yaml_file) as fl:
                data = yaml.load(fl, Loader=yaml.FullLoader)
        elif isinstance(yaml_file, collections.abc.Mapping):
            data = yaml_file
        else:
            raise ValueError(
                "yaml_file must be a string filepath or a raw dict from such a file."
            )

        antpos = data.pop("antpos")

        if isinstance(antpos, dict):
            fnc = getattr(antpos_module, antpos.pop("function"))
            antpos = fnc(**antpos)

        elif isinstance(antpos, str):
            if antpos.endswith(".npy"):
                antpos = np.load(antpos)
            else:
                try:
                    antpos = np.genfromtxt(antpos)
                except Exception:
                    raise TypeError("None of the loaders for antpos worked.")

        try:
            antpos = np.array(antpos)
        except ValueError:
            raise ValueError(
                "antpos must be a function from antpos, or a .npy or ascii "
                "file, or convertible to a ndarray"
            )

        _beam = data.pop("beam")
        kind = _beam.pop("class")
        _beam = getattr(beam, kind)(**_beam)

        return cls(antpos=antpos, beam=_beam, **data)

    @staticmethod
    def beamgridder(xcen, ycen, uvgrid, n):
        """A function for updating a uvgrid with a baseline group

        Parameters
        ----------
        xcen, ycen: float
            The (x,y)-components of the baseline to be gridded, in units of the
            `uvgrid` resolution.
        uvgrid : array
            The array to update with the new baseline group
        n : int
            Number of baselines in the baseline group.
        """
        size = uvgrid.shape[0]
        cen = size // 2  # - 0.5  # correction for centering
        xcen += cen
        ycen += cen

        if not (
            round(ycen) > size - 1 or round(xcen) > size - 1 or ycen < 0 or xcen < 0
        ):
            uvgrid[int(round(ycen)), int(round(xcen))] += n  # single pixel gridder

    @cached_property
    def baselines_metres(self):
        """
        The raw baseline distances in metres for every pair of antennas (shape (Nant, Nant, 3)).
        """
        # this does an "outer" subtraction, leaving the inner 2- or 3- length positions
        # as atomic quantities.
        return self.antpos[np.newaxis, :, :] - self.antpos[:, np.newaxis, :]

    def projected_baselines(self, baselines=None, time_offset=0):
        """The *projected* baseline lengths (in wavelengths) phased to a point
        that has rotated off zenith by some time_offset.

        Parameters
        ----------
        baselines : array_like, optional
            The baseline co-ordinates to project, assumed to be in metres.
            If not provided, uses all baselines of the observatory.
            Shape of the array can be (N,N,3) or (N, 3).
            The co-ordinates are expected to be in ENU.
        time_offset : float or Quantity
            The amount of time elapsed since the phase center was at zenith.
            Assumed to be in days unless otherwise defined. May be negative.

        Returns
        -------
        An array the same shape as :attr:`baselines_metres`, but phased to the
        new phase centre.
        """
        if baselines is None:
            baselines = self.baselines_metres

        baselines = ut.apply_or_convert_unit("m")(baselines)
        orig_shape = baselines.shape

        bl_wavelengths = baselines.reshape((-1, 3)) * self.metres_to_wavelengths

        out = ut.phase_past_zenith(time_offset, bl_wavelengths, self.latitude)
        return out.reshape(orig_shape)

    @cached_property
    def metres_to_wavelengths(self):
        """Conversion factor for converting a quantity in m to wavelengths at the fiducial
        frequency of the observation"""
        return (self.frequency / cnst.c).to("1/m")

    @cached_property
    def baseline_lengths(self):
        """Lengths of baselines in units of wavelengths, shape (Nant, Nant)"""
        return np.sqrt(np.sum(self.projected_baselines() ** 2, axis=-1))

    @cached_property
    def shortest_baseline(self):
        """Shortest baseline in units of wavelengths"""
        return np.min(self.baseline_lengths[self.baseline_lengths > 0])

    @cached_property
    def longest_baseline(self):
        """Longest baseline in units of wavelengths"""
        return np.max(self.baseline_lengths)

    @cached_property
    def observation_duration(self):
        """The time it takes for the sky to drift through the FWHM"""
        return units.day * self.beam.fwhm() / (2 * np.pi * units.rad)

    def get_redundant_baselines(self, bl_min=0, bl_max=np.inf, ndecimals=1):
        """
        Determine all baseline groups.

        Parameters
        ----------
        bl_min : float or astropy.Quantity, optional
            The minimum baseline to consider, in metres (or compatible units)
        bl_max : float or astropy.Quantity, optional
            The maximum baseline to consider, in metres (or compatible units)
        ndecimals : int, optional
            The number of decimals to which the UV points must be the same to be
            considered redundant.

        Returns
        -------
        dict: a dictionary in which keys are 3-tuples of (u,v, |u|) co-ordinates and
            values are lists of 2-tuples, where each 2-tuple consists of the indices
            of a pair of antennas with those co-ordinates.
        """
        uvbins = defaultdict(list)

        bl_min = ut.apply_or_convert_unit("m")(bl_min) * self.metres_to_wavelengths
        bl_max = ut.apply_or_convert_unit("m")(bl_max) * self.metres_to_wavelengths

        uvw = self.projected_baselines()
        # group redundant baselines
        for i in tqdm.tqdm(
            range(self.n_antennas - 1),
            desc="finding redundancies",
            unit="ants",
            disable=not config.PROGRESS,
        ):
            for j in range(i + 1, self.n_antennas):

                bl_len = self.baseline_lengths[i, j]  # in wavelengths
                if bl_len < bl_min or bl_len > bl_max:
                    continue

                u, v = uvw[i, j][:2]

                uvbin = (
                    ut.trunc(u, ndecimals=ndecimals),
                    ut.trunc(v, ndecimals=ndecimals),
                    ut.trunc(bl_len, ndecimals=ndecimals),
                )

                # add the uv point and its inverse to the redundant baseline dict.
                uvbins[uvbin].append((i, j))
                uvbins[(-uvbin[0], -uvbin[1], uvbin[2])].append((j, i))

        return uvbins

    def time_offsets_from_obs_int_time(
        self, integration_time, observation_duration=None
    ):
        """
        Compute a list of time offsets within an LST-bin (i.e. they are added coherently for
        a given baseline group).

        Time offsets are with respect to an arbitrary time, and describe the rotation of
        a hypothetical point through zenith.

        Parameters
        ----------
        integration_time : float or astropy.Quantity
            Time for single snapshot, assumed to be in seconds.
        observation_duration : float or astropy.Quantity
            Duration of the LST bin (for single night). Assumed to be in minutes.

        Returns
        -------
        array :
            Time offsets (in julian days).
        """
        if observation_duration is None:
            observation_duration = self.observation_duration

        observation_duration = ut.apply_or_convert_unit("min")(observation_duration)
        integration_time = ut.apply_or_convert_unit("s")(integration_time)
        assert integration_time <= observation_duration

        return np.arange(
            -observation_duration.to("day").value / 2,
            observation_duration.to("day").value / 2,
            integration_time.to("day").value,
        )

    def baseline_coords_from_groups(self, baseline_groups):
        """Convert a dictionary of baseline groups to an array of ENU co-ordinates"""
        out = np.zeros((len(baseline_groups), 3)) * units.m
        for i, antpairs in enumerate(baseline_groups.values()):
            out[i] = self.baselines_metres[antpairs[0][0], antpairs[0][1]]
        return out

    @staticmethod
    def baseline_weights_from_groups(baseline_groups):
        return np.array([len(antpairs) for antpairs in baseline_groups.values()])

    def grid_baselines(
        self,
        baselines=None,
        weights=None,
        integration_time=60.0 * units.s,
        bl_min=0,
        bl_max=np.inf,
        observation_duration=None,
        ndecimals=0,
    ):
        """
        Grid baselines onto a pre-determined uvgrid, accounting for earth rotation.

        Parameters
        ----------
        baselines : array_like, optional
            The baseline co-ordinates to project, assumed to be in metres.
            If not provided, calculates effective baselines by finding redundancies on
            all baselines in the observatory. Shape of the array can be (N,N,3) or (N, 3).
            The co-ordinates are expected to be in ENU. If `baselines` is provided,
            `weights` must also be provided.
        weights: array_like, optional
            An array of the same length as `baselines`, giving the number of independent
            baselines at each co-ordinate. If not provided, calculates effective
            baselines by finding redundancies on all baselines in the observatory.
            If `baselines` is provided, `weights` must also be provided.
        integration_time : float or Quantity, optional
            The amount of time integrated into a snapshot visibility, assumed
            to be in seconds.
        bl_min : float or Quantity, optional
            Minimum baseline length (in meters) to include in the gridding.
        bl_max : float or Quantity, optional
            Maximum baseline length (in meters) to include in the gridding.
        observation_duration : float or Quantity, optional
            Amount of time in a single (coherent) LST bin, assumed to be in minutes.
        ndecimals : int, optional
            Number of decimals to which baselines must match to be considered redundant.

        Returns
        -------
        array :
            Shape [n_baseline_groups, Nuv, Nuv]. The coherent sum of baselines within
            grid cells given by :attr:`ugrid`. One can treat different baseline groups
            independently, or sum over them.

        See Also
        --------
        grid_baselines_coherent :
            Coherent sum over baseline groups of the output of this method.
        grid_basleine_incoherent :
            Incoherent sum over baseline groups of the output of this method.
        """
        if baselines is None:
            baseline_groups = self.get_redundant_baselines(
                bl_min=bl_min, bl_max=bl_max, ndecimals=ndecimals
            )
            baselines = self.baseline_coords_from_groups(baseline_groups)
            weights = self.baseline_weights_from_groups(baseline_groups)

        if weights is None:
            raise ValueError(
                "If baselines are provided, weights must also be provided."
            )

        time_offsets = self.time_offsets_from_obs_int_time(
            integration_time, observation_duration
        )

        # Get all UVWs ahead of time.
        uvws = np.empty((len(baselines), len(time_offsets), 3))
        for itime, time_offset in enumerate(
            tqdm.tqdm(
                time_offsets,
                desc="computing UVWs",
                unit="times",
                disable=not config.PROGRESS,
            )
        ):
            uvws[:, itime, :] = self.projected_baselines(baselines, time_offset)

        # grid each baseline type into uv plane
        dim = len(self.ugrid(bl_max))
        edges = self.ugrid_edges(bl_max)

        uvsum = np.zeros((len(baselines), dim, dim))
        for cnt, (uvw, nbls) in enumerate(
            tqdm.tqdm(
                zip(uvws, weights),
                desc="gridding baselines",
                unit="baselines",
                disable=not config.PROGRESS,
                total=len(weights),
            )
        ):
            uvsum[cnt] = np.histogram2d(uvw[:, 0], uvw[:, 1], bins=edges)[0] * nbls

        return uvsum

    def longest_used_baseline(self, bl_max=np.inf):
        """Determine the maximum baseline length kept in the array"""
        if np.isinf(bl_max):
            return self.longest_baseline
        else:
            bl_max = ut.apply_or_convert_unit("m")(bl_max) * self.metres_to_wavelengths
            return np.max(self.baseline_lengths[self.baseline_lengths <= bl_max])

    def ugrid_edges(self, bl_max=np.inf):
        """
        Generate a uv grid out to the maximum used baseline smaller than the given bl_max.
        The resulting array represents the *edges* of the grid (so the number of cells
        is one fewer than this).

        Parameters
        ----------
        bl_max : float or Quantity
            Include all baselines smaller than this number. Units of m.

        Returns
        -------
        array :
            1D array of regularly spaced u.
        """
        bl_max = self.longest_used_baseline(bl_max)

        # We're doing edges of bins here, and the first edge is at uv_res/2
        n_positive = int(
            np.ceil((bl_max - self.beam.uv_resolution / 2) / self.beam.uv_resolution)
        )

        # Grid from uv_res/2 to just past (or equal to) bl_max, in steps of resolution.
        positive = np.linspace(
            self.beam.uv_resolution / 2,
            self.beam.uv_resolution / 2 + n_positive * self.beam.uv_resolution,
            n_positive + 1,
        )
        edges = np.concatenate((-positive[::-1], positive))
        return edges

    def ugrid(self, bl_max=np.inf):
        """Centres of the UV grid plane."""
        # Shift the edges by half a cell, and omit the last one
        edges = self.ugrid_edges(bl_max)
        return (edges[1:] + edges[:-1]) / 2

    def grid_baselines_coherent(self, **kwargs):
        """
        Produce a UV grid of gridded baselines, where different baseline
        groups are averaged coherently if they fall into the same UV bin.

        See :func:`grid_baselines` for parameter details.
        """
        grid = self.grid_baselines(**kwargs)
        return np.sum(grid, axis=0)

    def grid_baselines_incoherent(self, **kwargs):
        """
        Produce a UV grid of gridded baselines, where different baseline
        groups are averaged incoherently if they fall into the same UV bin.

        See :func:`grid_baselines` for parameter details.
        """
        grid = self.grid_baselines(**kwargs)
        return np.sqrt(np.sum(grid ** 2, axis=0))

    def __eq__(self, other):
        if not self.__class__ == other.__class__:
            return False
        if not (self.Trcv, self.beam, self.latitude) == (
            other.Trcv,
            other.beam,
            other.latitude,
        ):
            return False

        if not np.array_equal(self.antpos.value, other.antpos.value):
            return False

        return True
