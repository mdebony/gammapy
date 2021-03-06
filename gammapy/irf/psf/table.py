# Licensed under a 3-clause BSD style license - see LICENSE.rst
import logging
import numpy as np
from astropy import units as u
from astropy.coordinates import Angle
from astropy.io import fits
from astropy.table import Table
from gammapy.maps import MapAxis, MapAxes
from gammapy.utils.array import array_stats_str
from gammapy.utils.nddata import NDDataArray
from gammapy.utils.gauss import Gauss2DPDF
from gammapy.utils.scripts import make_path

__all__ = ["TablePSF", "EnergyDependentTablePSF", "PSF3D"]

log = logging.getLogger(__name__)


class TablePSF:
    """Radially-symmetric table PSF.

    Parameters
    ----------
    rad_axis : `~astropy.units.Quantity` with angle units
        Offset wrt source position
    data : `~astropy.units.Quantity` with sr^-1 units
        PSF value array
    interp_kwargs : dict
        Keyword arguments passed to `ScaledRegularGridInterpolator`
    """

    def __init__(self, rad_axis, data, interp_kwargs=None):
        interp_kwargs = interp_kwargs or {}

        rad_axis.assert_name("rad")

        self.data = NDDataArray(
            axes=[rad_axis], data=u.Quantity(data).to("sr^-1"), interp_kwargs=interp_kwargs
        )

    @property
    def rad_axis(self):
        return self.data.axes["rad"]

    @classmethod
    def from_shape(cls, shape, width, rad):
        """Make TablePSF objects with commonly used shapes.

        This function is mostly useful for examples and testing.

        Parameters
        ----------
        shape : {'disk', 'gauss'}
            PSF shape.
        width : `~astropy.units.Quantity` with angle units
            PSF width angle (radius for disk, sigma for Gauss).
        rad : `~astropy.units.Quantity` with angle units
            Offset angle

        Returns
        -------
        psf : `TablePSF`
            Table PSF

        Examples
        --------
        >>> import numpy as np
        >>> from astropy.coordinates import Angle
        >>> from gammapy.irf import TablePSF
        >>> rad = Angle(np.linspace(0, 0.7, 100), 'deg')
        >>> psf = TablePSF.from_shape(shape='gauss', width='0.2 deg', rad=rad)
        """
        width = Angle(width)
        rad = Angle(rad)

        if shape == "disk":
            amplitude = 1 / (np.pi * width.radian ** 2)
            data = np.where(rad < width, amplitude, 0)
        elif shape == "gauss":
            gauss2d_pdf = Gauss2DPDF(sigma=width.radian)
            data = gauss2d_pdf(rad.radian)
        else:
            raise ValueError(f"Invalid shape: {shape}")

        data = u.Quantity(data, "sr^-1")
        rad_axis = MapAxis.from_nodes(rad, name="rad")
        return cls(rad_axis=rad_axis, data=data)

    def info(self):
        """Print basic info."""
        ss = array_stats_str(self.rad_axis.center, "offset")
        ss += f"integral = {self.containment(self.rad_axis.edges[-1])}\n"

        for containment in [68, 80, 95]:
            radius = self.containment_radius(0.01 * containment)
            ss += f"containment radius {radius.deg} deg for {containment}%\n"

        return ss

    def evaluate(self, rad):
        r"""Evaluate PSF.

        The following PSF quantities are available:

        * 'dp_domega': PDF per 2-dim solid angle :math:`\Omega` in sr^-1

            .. math:: \frac{dP}{d\Omega}


        Parameters
        ----------
        rad : `~astropy.coordinates.Angle`
            Offset wrt source position

        Returns
        -------
        psf_value : `~astropy.units.Quantity`
            PSF value
        """
        return self.data.evaluate(rad=rad)

    def containment(self, rad_max):
        """Compute PSF containment fraction.

        Parameters
        ----------
        rad_max : `~astropy.units.Quantity`
            Offset angle range

        Returns
        -------
        integral : float
            PSF integral
        """
        rad_max = np.atleast_1d(rad_max)
        return self.data._integrate_rad((rad_max,))

    def containment_radius(self, fraction):
        """Containment radius.

        Parameters
        ----------
        fraction : array_like
            Containment fraction (range 0 .. 1)

        Returns
        -------
        rad : `~astropy.coordinates.Angle`
            Containment radius angle
        """
        # TODO: check whether starting
        rad_max = Angle(
            np.linspace(0 * u.deg, self.rad_axis.center[-1], 10 * self.rad_axis.nbin),
            "rad",
        )

        containment = self.containment(rad_max=rad_max)

        fraction = np.atleast_1d(fraction)

        fraction_idx = np.argmin(np.abs(containment - fraction[:, np.newaxis]), axis=1)
        return rad_max[fraction_idx].to("deg")

    def normalize(self):
        """Normalize PSF to unit integral.

        Computes the total PSF integral via the :math:`dP / dr` spline
        and then divides the :math:`dP / dr` array.
        """
        integral = self.containment(self.rad_axis.edges[-1])
        self.data /= integral

    def plot_psf_vs_rad(self, ax=None, **kwargs):
        """Plot PSF vs radius.

        Parameters
        ----------
        ax : ``

        kwargs : dict
            Keyword arguments passed to `matplotlib.pyplot.plot`
        """
        import matplotlib.pyplot as plt

        ax = plt.gca() if ax is None else ax

        ax.plot(
            self.rad_axis.center.to_value("deg"),
            self.data.data.to_value("sr-1"),
            **kwargs,
        )
        ax.set_yscale("log")
        ax.set_xlabel("Radius (deg)")
        ax.set_ylabel("PSF (sr-1)")


class EnergyDependentTablePSF:
    """Energy-dependent radially-symmetric table PSF (``gtpsf`` format).

    TODO: add references and explanations.

    Parameters
    ----------
    energy_axis_true : `MapAxis`
        Energy axis
    rad_axis : `MapAxis`
        Offset angle wrt source position axis
    exposure : `~astropy.units.Quantity`
        Exposure (1-dim)
    data : `~astropy.units.Quantity`
        PSF (2-dim with axes: psf[energy_index, offset_index]
    interp_kwargs : dict
        Interpolation keyword arguments pass to `ScaledRegularGridInterpolator`.
    """

    def __init__(
        self,
        energy_axis_true,
        rad_axis,
        exposure=None,
        data=None,
        interp_kwargs=None,
    ):
        interp_kwargs = interp_kwargs or {}
        axes = MapAxes([energy_axis_true, rad_axis])
        axes.assert_names(["energy_true", "rad"])

        self.data = NDDataArray(
            axes=axes, data=u.Quantity(data).to("sr^-1"), interp_kwargs=interp_kwargs
        )

        if exposure is None:
            self.exposure = u.Quantity(np.ones(self.energy_axis_true.nbin), "cm^2 s")
        else:
            self.exposure = u.Quantity(exposure).to("cm^2 s")

    @property
    def energy_axis_true(self):
        return self.data.axes["energy_true"]

    @property
    def rad_axis(self):
        return self.data.axes["rad"]

    def __str__(self):
        ss = "EnergyDependentTablePSF\n"
        ss += "-----------------------\n"
        ss += "\nAxis info:\n"
        ss += "  " + array_stats_str(self.rad_axis.center.to("deg"), "rad")
        ss += "  " + array_stats_str(self.energy_axis_true.center, "energy")
        ss += "\nContainment info:\n"
        # Print some example containment radii
        fractions = [0.68, 0.95]
        energies = u.Quantity([10, 100], "GeV")
        for fraction in fractions:
            rads = self.containment_radius(energy=energies, fraction=fraction)
            for energy, rad in zip(energies, rads):
                ss += f"  {100 * fraction}% containment radius at {energy:3.0f}: {rad:.2f}\n"

        return ss

    @classmethod
    def from_hdulist(cls, hdu_list):
        """Create `EnergyDependentTablePSF` from ``gtpsf`` format HDU list.

        Parameters
        ----------
        hdu_list : `~astropy.io.fits.HDUList`
            HDU list with ``THETA`` and ``PSF`` extensions.
        """
        # TODO: move this to MapAxis.from_table()
        rad = Angle(hdu_list["THETA"].data["Theta"], "deg")
        rad_axis = MapAxis.from_nodes(rad, name="rad")
        energy = u.Quantity(hdu_list["PSF"].data["Energy"], "MeV")
        energy_axis_true = MapAxis.from_nodes(energy, name="energy_true", interp="log")
        exposure = u.Quantity(hdu_list["PSF"].data["Exposure"], "cm^2 s")
        data = u.Quantity(hdu_list["PSF"].data["PSF"], "sr^-1")
        return cls(
            energy_axis_true=energy_axis_true,
            rad_axis=rad_axis,
            exposure=exposure,
            data=data,
        )

    def to_hdulist(self):
        """Convert to FITS HDU list format.

        Returns
        -------
        hdu_list : `~astropy.io.fits.HDUList`
            PSF in HDU list format.
        """
        theta_hdu = self.rad_axis.to_table_hdu(format="gtpsf")

        psf_table = self.energy_axis_true.to_table(format="gtpsf")
        psf_table["Exposure"] = self.exposure.to("cm^2 s")
        psf_table["PSF"] = self.data.data.to("sr^-1")
        psf_hdu = fits.BinTableHDU(data=psf_table, name="PSF")

        return fits.HDUList([fits.PrimaryHDU(), theta_hdu, psf_hdu])

    @classmethod
    def read(cls, filename):
        """Create `EnergyDependentTablePSF` from ``gtpsf``-format FITS file.

        Parameters
        ----------
        filename : str
            File name
        """
        with fits.open(str(make_path(filename)), memmap=False) as hdulist:
            return cls.from_hdulist(hdulist)

    def write(self, filename, *args, **kwargs):
        """Write to FITS file.

        Calls `~astropy.io.fits.HDUList.writeto`, forwarding all arguments.
        """
        self.to_hdulist().writeto(str(make_path(filename)), *args, **kwargs)

    def evaluate(self, energy=None, rad=None, method="linear"):
        """Evaluate the PSF at a given energy and offset

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energy value
        rad : `~astropy.coordinates.Angle`
            Offset wrt source position
        method : {"linear", "nearest"}
            Linear or nearest neighbour interpolation.

        Returns
        -------
        values : `~astropy.units.Quantity`
            Interpolated value
        """
        if energy is None:
            energy = self.energy_axis_true.center

        if rad is None:
            rad = self.rad_axis.center

        energy = u.Quantity(energy, ndmin=1)[:, np.newaxis]
        rad = u.Quantity(rad, ndmin=1)
        return self.data._interpolate((energy, rad), method=method)

    def table_psf_at_energy(self, energy, method="linear", **kwargs):
        """Create `~gammapy.irf.TablePSF` at one given energy.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energy
        method : {"linear", "nearest"}
            Linear or nearest neighbour interpolation.

        Returns
        -------
        psf : `~gammapy.irf.TablePSF`
            Table PSF
        """
        psf_value = self.evaluate(energy=energy, method=method)[0, :]
        return TablePSF(rad_axis=self.rad_axis, data=psf_value, **kwargs)

    def table_psf_in_energy_range(
        self, energy_range, spectrum=None, n_bins=11, **kwargs
    ):
        """Average PSF in a given energy band.

        Expected counts in sub energy bands given the given exposure
        and spectrum are used as weights.

        Parameters
        ----------
        energy_range : `~astropy.units.Quantity`
            Energy band
        spectrum : `~gammapy.modeling.models.SpectralModel`
            Spectral model used for weighting the PSF. Default is a power law
            with index=2.
        n_bins : int
            Number of energy points in the energy band, used to compute the
            weigthed PSF.

        Returns
        -------
        psf : `TablePSF`
            Table PSF
        """
        from gammapy.modeling.models import PowerLawSpectralModel, TemplateSpectralModel

        if spectrum is None:
            spectrum = PowerLawSpectralModel()

        exposure = TemplateSpectralModel(self.energy_axis_true.center, self.exposure)

        e_min, e_max = energy_range
        energy = MapAxis.from_energy_bounds(e_min, e_max, n_bins).edges

        weights = spectrum(energy) * exposure(energy)
        weights /= weights.sum()

        psf_value = self.evaluate(energy=energy)
        psf_value_weighted = weights[:, np.newaxis] * psf_value
        return TablePSF(self.rad_axis, psf_value_weighted.sum(axis=0), **kwargs)

    def containment_radius(self, energy, fraction=0.68):
        """Containment radius.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energy
        fraction : float
            Containment fraction.

        Returns
        -------
        rad : `~astropy.units.Quantity`
            Containment radius in deg
        """
        # upsamle for better precision
        rad_max = Angle(self.rad_axis.upsample(factor=10).center)
        containment = self.containment(energy=energy, rad_max=rad_max)

        # find nearest containment value
        fraction_idx = np.argmin(np.abs(containment - fraction), axis=1)
        return rad_max[fraction_idx].to("deg")

    def containment(self, energy, rad_max):
        """Compute containment of the PSF.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energy
        rad_max : `~astropy.coordinates.Angle`
            Maximum offset angle.

        Returns
        -------
        fraction : array_like
            Containment fraction (in range 0 .. 1)
        """
        energy = np.atleast_1d(u.Quantity(energy))[:, np.newaxis]
        rad_max = np.atleast_1d(u.Quantity(rad_max))
        return self.data._integrate_rad((energy, rad_max))

    def info(self):
        """Print basic info"""
        print(str(self))

    def plot_psf_vs_rad(self, energy=None, ax=None, **kwargs):
        """Plot PSF vs radius.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energies where to plot the PSF.
        **kwargs : dict
            Keyword arguments pass to `~matplotlib.pyplot.plot`.
        """
        import matplotlib.pyplot as plt

        if energy is None:
            energy = [100, 1000, 10000] * u.GeV

        ax = plt.gca() if ax is None else ax

        for value in energy:
            psf_value = np.squeeze(self.evaluate(energy=value))
            label = f"{value:.0f}"
            ax.plot(
                self.rad_axis.center.to_value("deg"),
                psf_value.to_value("sr-1"),
                label=label,
                **kwargs,
            )

        ax.set_yscale("log")
        ax.set_xlabel("Offset (deg)")
        ax.set_ylabel("PSF (1 / sr)")
        plt.legend()
        return ax

    def plot_containment_vs_energy(
        self, ax=None, fractions=[0.68, 0.8, 0.95], **kwargs
    ):
        """Plot containment versus energy."""
        import matplotlib.pyplot as plt

        ax = plt.gca() if ax is None else ax

        for fraction in fractions:
            rad = self.containment_radius(self.energy_axis_true.center, fraction)
            label = f"{100 * fraction:.1f}% Containment"
            ax.plot(
                self.energy_axis_true.center.to("GeV").value,
                rad.to("deg").value,
                label=label,
                **kwargs,
            )

        ax.semilogx()
        ax.legend(loc="best")
        ax.set_xlabel("Energy (GeV)")
        ax.set_ylabel("Containment radius (deg)")

    def plot_exposure_vs_energy(self):
        """Plot exposure versus energy."""
        import matplotlib.pyplot as plt

        plt.figure(figsize=(4, 3))
        plt.plot(self.energy_axis_true.center, self.exposure, color="black", lw=3)
        plt.semilogx()
        plt.xlabel("Energy (MeV)")
        plt.ylabel("Exposure (cm^2 s)")
        plt.xlim(1e4 / 1.3, 1.3 * 1e6)
        plt.ylim(0, 1.5e11)
        plt.tight_layout()


class PSF3D:
    """PSF with axes: energy, offset, rad.

    Data format specification: :ref:`gadf:psf_table`

    Parameters
    ----------
    energy_axis_true : `MapAxis`
        True energy axis.
    offset_axis : `MapAxis`
        Offset axis
    rad_axis : `MapAxis`
        Rad axis
    data : `~astropy.units.Quantity`
        PSF (3-dim with axes: psf[rad_index, offset_index, energy_index]
    meta : dict
        Meta dict
    """

    tag = "psf_table"

    def __init__(
        self,
        energy_axis_true,
        offset_axis,
        rad_axis,
        data,
        meta=None,
        interp_kwargs=None,
    ):

        interp_kwargs = interp_kwargs or {}

        axes = MapAxes([energy_axis_true, offset_axis, rad_axis])
        axes.assert_names(["energy_true", "offset", "rad"])

        self.data = NDDataArray(
            axes=axes, data=u.Quantity(data).to("sr^-1"), interp_kwargs=interp_kwargs
        )

        self.meta = meta or {}

    @property
    def energy_thresh_lo(self):
        """Low energy threshold"""
        return self.meta["LO_THRES"] * u.TeV

    @property
    def energy_thresh_hi(self):
        """High energy threshold"""
        return self.meta["HI_THRES"] * u.TeV

    @property
    def energy_axis_true(self):
        return self.data.axes["energy_true"]

    @property
    def rad_axis(self):
        return self.data.axes["rad"]

    @property
    def offset_axis(self):
        return self.data.axes["offset"]

    def __repr__(self):
        """Print some basic info.
        """
        info = self.__class__.__name__ + "\n"
        info += "-" * len(self.__class__.__name__) + "\n\n"
        info += f"\tshape      : {self.data.data.shape}\n"
        return info

    @classmethod
    def read(cls, filename, hdu="PSF_2D_TABLE"):
        """Create `PSF3D` from FITS file.

        Parameters
        ----------
        filename : str
            File name
        hdu : str
            HDU name
        """
        table = Table.read(make_path(filename), hdu=hdu)
        return cls.from_table(table)

    @classmethod
    def from_table(cls, table):
        """Create `PSF3D` from `~astropy.table.Table`.

        Parameters
        ----------
        table : `~astropy.table.Table`
            Table Table-PSF info.
        """
        axes = MapAxes.from_table(
            table=table, column_prefixes=["ENERG", "THETA", "RAD"], format="gadf-dl3"
        )

        data = table["RPSF"].quantity[0].transpose()
        return cls(
            energy_axis_true=axes["energy_true"],
            offset_axis=axes["offset"],
            rad_axis=axes["rad"],
            data=data,
            meta=table.meta
        )

    def to_hdulist(self):
        """Convert PSF table data to FITS HDU list.

        Returns
        -------
        hdu_list : `~astropy.io.fits.HDUList`
            PSF in HDU list format.
        """
        table = self.data.axes.to_table(format="gadf-dl3")

        table["RPSF"] = self.data.data.T[np.newaxis]

        hdu = fits.BinTableHDU(table)
        hdu.header["LO_THRES"] = self.energy_thresh_lo.value
        hdu.header["HI_THRES"] = self.energy_thresh_hi.value

        return fits.HDUList([fits.PrimaryHDU(), hdu])

    def write(self, filename, *args, **kwargs):
        """Write PSF to FITS file.

        Calls `~astropy.io.fits.HDUList.writeto`, forwarding all arguments.
        """
        self.to_hdulist().writeto(str(make_path(filename)), *args, **kwargs)

    def evaluate(self, energy=None, offset=None, rad=None):
        """Interpolate PSF value at a given offset and energy.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            energy value
        offset : `~astropy.coordinates.Angle`
            Offset in the field of view
        rad : `~astropy.coordinates.Angle`
            Offset wrt source position

        Returns
        -------
        values : `~astropy.units.Quantity`
            Interpolated value
        """
        if energy is None:
            energy = self.energy_axis_true.center
        if offset is None:
            offset = self.offset_axis.center
        if rad is None:
            rad = self.rad_axis.center

        rad = np.atleast_1d(u.Quantity(rad))
        offset = np.atleast_1d(u.Quantity(offset))
        energy = np.atleast_1d(u.Quantity(energy))
        return self.data._interpolate(
            (
                energy[np.newaxis, np.newaxis, :],
                offset[np.newaxis, :, np.newaxis],
                rad[:, np.newaxis, np.newaxis],
            )
        )

    def to_energy_dependent_table_psf(self, theta="0 deg", rad=None, exposure=None):
        """
        Convert PSF3D in EnergyDependentTablePSF.

        Parameters
        ----------
        theta : `~astropy.coordinates.Angle`
            Offset in the field of view
        rad : `~astropy.coordinates.Angle`
            Offset from PSF center used for evaluating the PSF on a grid.
            Default is the ``rad`` from this PSF.
        exposure : `~astropy.units.Quantity`
            Energy dependent exposure. Should be in units equivalent to 'cm^2 s'.
            Default exposure = 1.

        Returns
        -------
        table_psf : `~gammapy.irf.EnergyDependentTablePSF`
            Energy-dependent PSF
        """
        theta = Angle(theta)

        if rad is not None:
            rad_axis = MapAxis.from_edges(rad, name="rad")
        else:
            rad_axis = self.rad_axis

        psf_value = self.evaluate(offset=theta, rad=rad_axis.center).squeeze()
        return EnergyDependentTablePSF(
            energy_axis_true=self.energy_axis_true,
            rad_axis=rad_axis,
            exposure=exposure,
            data=psf_value.transpose(),
        )

    def to_table_psf(self, energy, theta="0 deg", **kwargs):
        """Create `~gammapy.irf.TablePSF` at one given energy.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energy
        theta : `~astropy.coordinates.Angle`
            Offset in the field of view. Default theta = 0 deg

        Returns
        -------
        psf : `~gammapy.irf.TablePSF`
            Table PSF
        """
        energy = u.Quantity(energy)
        theta = Angle(theta)
        psf_value = self.evaluate(energy, theta).squeeze()
        return TablePSF(rad_axis=self.rad_axis, data=psf_value, **kwargs)

    def containment_radius(
        self, energy, theta="0 deg", fraction=0.68
    ):
        """Containment radius.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energy
        theta : `~astropy.coordinates.Angle`
            Offset in the field of view. Default theta = 0 deg
        fraction : float
            Containment fraction. Default fraction = 0.68

        Returns
        -------
        radius : `~astropy.units.Quantity`
            Containment radius in deg
        """
        energy = np.atleast_1d(u.Quantity(energy))
        theta = np.atleast_1d(u.Quantity(theta))

        radii = []
        for t in theta:
            psf = self.to_energy_dependent_table_psf(theta=t)
            radii.append(psf.containment_radius(energy, fraction=fraction))

        return u.Quantity(radii).T.squeeze()

    def plot_containment_vs_energy(
        self, fractions=[0.68, 0.95], thetas=Angle([0, 1], "deg"), ax=None, **kwargs
    ):
        """Plot containment fraction as a function of energy.
        """
        import matplotlib.pyplot as plt

        ax = plt.gca() if ax is None else ax

        energy = MapAxis.from_energy_bounds(
            self.energy_axis_true.edges[0], self.energy_axis_true.edges[-1], 100
        ).edges

        for theta in thetas:
            for fraction in fractions:
                plot_kwargs = kwargs.copy()
                radius = self.containment_radius(energy, theta, fraction)
                plot_kwargs.setdefault(
                    "label", f"{theta.deg} deg, {100 * fraction:.1f}%"
                )
                ax.plot(energy.value, radius.value, **plot_kwargs)

        ax.semilogx()
        ax.legend(loc="best")
        ax.set_xlabel("Energy (TeV)")
        ax.set_ylabel("Containment radius (deg)")

    def plot_psf_vs_rad(self, theta="0 deg", energy=u.Quantity(1, "TeV")):
        """Plot PSF vs rad.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            Energy. Default energy = 1 TeV
        theta : `~astropy.coordinates.Angle`
            Offset in the field of view. Default theta = 0 deg
        """
        theta = Angle(theta)
        table = self.to_table_psf(energy=energy, theta=theta)
        return table.plot_psf_vs_rad()

    def plot_containment(self, fraction=0.68, ax=None, add_cbar=True, **kwargs):
        """Plot containment image with energy and theta axes.

        Parameters
        ----------
        fraction : float
            Containment fraction between 0 and 1.
        add_cbar : bool
            Add a colorbar
        """
        import matplotlib.pyplot as plt

        ax = plt.gca() if ax is None else ax

        energy = self.energy_axis_true.center
        offset = self.offset_axis.center

        # Set up and compute data
        containment = self.containment_radius(energy, offset, fraction)

        # plotting defaults
        kwargs.setdefault("cmap", "GnBu")
        kwargs.setdefault("vmin", np.nanmin(containment.value))
        kwargs.setdefault("vmax", np.nanmax(containment.value))

        # Plotting
        x = energy.value
        y = offset.value
        caxes = ax.pcolormesh(x, y, containment.value.T, **kwargs)

        # Axes labels and ticks, colobar
        ax.semilogx()
        ax.set_ylabel(f"Offset ({offset.unit})")
        ax.set_xlabel(f"Energy ({energy.unit})")
        ax.set_xlim(x.min(), x.max())
        ax.set_ylim(y.min(), y.max())

        try:
            self._plot_safe_energy_range(ax)
        except KeyError:
            pass

        if add_cbar:
            label = f"Containment radius R{100 * fraction:.0f} ({containment.unit})"
            ax.figure.colorbar(caxes, ax=ax, label=label)

        return ax

    def _plot_safe_energy_range(self, ax):
        """add safe energy range lines to the plot"""
        esafe = self.energy_thresh_lo
        omin = self.offset_axis.center.value.min()
        omax = self.offset_axis.center.value.max()
        ax.vlines(x=esafe.value, ymin=omin, ymax=omax)
        label = f"Safe energy threshold: {esafe:3.2f}"
        ax.text(x=0.1, y=0.9 * esafe.value, s=label, va="top")

    def peek(self, figsize=(15, 5)):
        """Quick-look summary plots."""
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(nrows=1, ncols=3, figsize=figsize)

        self.plot_containment(fraction=0.68, ax=axes[0])
        self.plot_containment(fraction=0.95, ax=axes[1])
        self.plot_containment_vs_energy(ax=axes[2])

        # TODO: implement this plot
        # psf = self.psf_at_energy_and_theta(energy='1 TeV', theta='1 deg')
        # psf.plot_components(ax=axes[2])

        plt.tight_layout()
