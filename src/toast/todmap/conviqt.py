# Copyright (c) 2015-2019 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

from ..mpi import use_mpi

import numpy as np

from .. import qarray as qa

from ..op import Operator

from ..timing import function_timer, Timer

conviqt = None

if use_mpi:
    try:
        import libconviqt_wrapper as conviqt
    except ImportError:
        pass


class OpSimConviqt(Operator):
    """Operator which uses libconviqt to generate beam-convolved timestreams.

    This passes through each observation and loops over each detector.
    For each detector, it produces the beam-convolved timestream.

    Args:
        comm (MPI.Comm) : MPI communicator to use for the convolution.
            libConviqt does not work without MPI.
        sky_file (str) : File containing the sky a_lm expansion.
            Tag "DETECTOR" will be replaced with the detector name
        beam_file (str) : File containing the beam a_lm expansion.
            Tag "DETECTOR" will be replaced with the detector name
        lmax (int) : Maximum ell (and m).  Actual resolution in the
            Healpix FITS file may differ.  If not set, will use the
            maximum expansion order from file.
        beammmax (int) : beam maximum m.  Actual resolution in the
            Healpix FITS file may differ.  If not set, will use the
            maximum expansion order from file. 
        pol (bool) : boolean to determine if polarized simulation is needed
        fwhm (float) : width of a symmetric gaussian beam [in arcmin] already
            present in the skyfile (will be deconvolved away).
        order (int) : conviqt order parameter (expert mode)
        calibrate (bool) : Calibrate intensity to 1.0, rather than (1+epsilon)/2
        dxx (bool) : The beam frame is either Dxx or Pxx.  Pxx includes the
            rotation to polarization sensitive basis, Dxx does not.  When
            Dxx=True, detector orientation from attitude quaternions is
            corrected for the polarization angle.
        out (str): the name of the cache object (<name>_<detector>) to
            use for output of the detector timestream.

    """

    def __init__(
        self,
        comm,
        sky_file,
        beam_file,
        lmax=0,
        beammmax=0,
        pol=True,
        fwhm=4.0,
        order=13,
        calibrate=True,
        dxx=True,
        out="conviqt",
        quat_name=None,
        flag_name=None,
        flag_mask=255,
        common_flag_name=None,
        common_flag_mask=255,
        apply_flags=False,
        remove_monopole=False,
        remove_dipole=False,
        normalize_beam=False,
        verbosity=0,
    ):
        # Call the parent class constructor
        super().__init__()

        self._comm = comm
        self._sky_file = sky_file
        self._beam_file = beam_file
        self._lmax = lmax
        self._beammmax = beammmax
        self._pol = pol
        self._fwhm = fwhm
        self._order = order
        self._calibrate = calibrate
        self._dxx = dxx
        self._quat_name = quat_name
        self._flag_name = flag_name
        self._flag_mask = flag_mask
        self._common_flag_name = common_flag_name
        self._common_flag_mask = common_flag_mask
        self._apply_flags = apply_flags
        self._remove_monopole = remove_monopole
        self._remove_dipole = remove_dipole
        self._normalize_beam = normalize_beam
        self._verbosity = verbosity

        self._out = out

    @property
    def available(self):
        """Return True if libconviqt is found in the library search path.
        """
        return conviqt is not None and conviqt.available

    @function_timer
    def exec(self, data):
        """Loop over all observations and perform the convolution.

        This is done one detector at a time.  For each detector, all data
        products are read from disk.

        Args:
            data (toast.Data): The distributed data.

        """
        if not self.available:
            raise RuntimeError("libconviqt is not available")

        timer = Timer()
        timer.start()

        detectors = self._get_detectors(data)

        for det in detectors:
            verbose = self._comm.rank == 0 and self._verbosity > 0

            sky_file = self._sky_file.replace("DETECTOR", det)
            sky = self.get_sky(sky_file, det, verbose)

            beam_file = self._beam_file.replace("DETECTOR", det)
            beam = self.get_beam(beam_file, det, verbose)

            detector = self.get_detector(det)

            theta, phi, psi = self.get_pointing(data, det, verbose)
            pnt = self.get_buffer(theta, phi, psi, det, verbose)
            del theta, phi, psi

            convolved_data = self.convolve(sky, beam, detector, pnt, det, verbose)

            self.calibrate(data, det, convolved_data, verbose)
            self.cache(data, det, convolved_data, verbose)

            del pnt, detector, beam, sky

            if verbose:
                timer.report_clear("conviqt process detector {}".format(det))

        return

    def _get_detectors(self, data):
        """ Assemble a list of detectors across all processes and
        observations in `self._comm`.
        """
        dets = set()
        for obs in data.obs:
            tod = obs["tod"]
            for det in tod.local_dets:
                dets.add(det)
        all_dets = self._comm.gather(dets, root=0)
        if self._comm.rank == 0:
            for some_dets in all_dets:
                dets.update(some_dets)
            dets = sorted(dets)
        all_dets = self._comm.bcast(dets, root=0)
        return all_dets

    def _get_psipol(self, focalplane, det):
        """ Parse polarization angle in radians from the focalplane
        dictionary.
        """
        if det not in focalplane:
            raise RuntimeError("focalplane does not include {}".format(det))
        if "pol_angle_deg" in focalplane:
            psipol = np.radians(focalplane[det]["pol_angle_deg"])
        elif "pol_angle_rad" in focalplane:
            psipol = focalplane[det]["pol_angle_rad"]
        else:
            raise RuntimeError("focalplane[{}] does not include psi".format(det))
        return psipol

    def _get_epsilon(self, focalplane, det):
        """ Parse polarization leakage (epsilon) from the focalplane
        object or dictionary.
        """
        if det not in focalplane:
            raise RuntimeError("focalplane does not include {}".format(det))
        if "pol_leakage" in focalplane[det]:
            epsilon = focalplane[det]["pol_leakage"]
        else:
            # Assume zero polarization leakage
            epsilon = 0
        return epsilon

    def get_sky(self, skyfile, det, verbose):
        timer = Timer()
        timer.start()
        sky = conviqt.Sky(self._lmax, self._pol, skyfile, self._fwhm, self._comm)
        if self._remove_monopole:
            sky.remove_monopole()
        if self._remove_dipole:
            sky.remove_dipole()
        if verbose:
            timer.report_clear("initialize sky for detector {}".format(det))
        return sky

    def get_beam(self, beamfile, det, verbose):
        timer = Timer()
        timer.start()
        beam = conviqt.Beam(self._lmax, self._beammmax, self._pol, beamfile, self._comm)
        if self._normalize_beam:
            beam.normalize()
        if verbose:
            timer.report_clear("initialize beam for detector {}".format(det))
        return beam

    def get_detector(self, det):
        """ We always create the detector with zero leakage and scale
        the returned TOD ourselves
        """
        detector = conviqt.Detector(name=det, epsilon=0)
        return detector

    def get_pointing(self, data, det, verbose):
        # We need the three pointing angles to describe the
        # pointing.  local_pointing() returns the attitude quaternions.
        nullquat = np.array([0, 0, 0, 1], dtype=np.float64)
        timer = Timer()
        timer.start()
        all_theta, all_phi, all_psi = [], [], []
        for obs in data.obs:
            tod = obs["tod"]
            if det not in tod.local_dets:
                continue
            focalplane = obs["focalplane"]
            quats = tod.local_pointing(det, self._quat_name)
            if verbose:
                timer.report_clear("get detector pointing for {}".format(det))

            if self._apply_flags:
                common = tod.local_common_flags(self._common_flag_name)
                flags = tod.local_flags(det, self._flag_name)
                common = common & self._common_flag_mask
                flags = flags & self._flag_mask
                totflags = np.copy(flags)
                totflags |= common
                quats = quats.copy()
                quats[totflags != 0] = nullquat
                if verbose:
                    timer.report_clear("initialize flags for detector {}".format(det))

            theta, phi, psi = qa.to_angles(quats)
            # Is the beam in Pxx or Dxx? Pxx will include the
            # detector polarization angle, Dxx will not.
            if self._dxx:
                psipol = self._get_psipol(focalplane, det)
                psi -= psipol
            all_theta.append(theta)
            all_phi.append(phi)
            all_psi.append(psi)
        if len(all_theta) > 0:
            all_theta = np.hstack(all_theta)
            all_phi = np.hstack(all_phi)
            all_psi = np.hstack(all_psi)
        if verbose:
            timer.report_clear("compute pointing angles for detector {}".format(det))
        return all_theta, all_phi, all_psi

    def get_buffer(self, theta, phi, psi, det, verbose):
        """Pack the pointing into the conviqt pointing array
        """
        timer = Timer()
        timer.start()
        pnt = conviqt.Pointing(len(theta))
        if pnt._nrow > 0:
            arr = pnt.data()
            arr[:, 0] = phi
            arr[:, 1] = theta
            arr[:, 2] = psi
        if verbose:
            timer.report_clear("pack input array for detector {}".format(det))
        return pnt

    def convolve(self, sky, beam, detector, pnt, det, verbose):
        timer = Timer()
        timer.start()
        convolver = conviqt.Convolver(
            sky,
            beam,
            detector,
            self._pol,
            self._lmax,
            self._beammmax,
            self._order,
            self._verbosity,
            self._comm,
        )
        convolver.convolve(pnt)
        if verbose:
            timer.report_clear("convolve detector {}".format(det))

        # The pointer to the data will have changed during
        # the convolution call ...

        if pnt._nrow > 0:
            arr = pnt.data()
            convolved_data = arr[:, 3].astype(np.float64)
        else:
            convolved_data = None
        if verbose:
            timer.report_clear("extract convolved data for {}".format(det))

        del convolver

        return convolved_data

    def calibrate(self, data, det, convolved_data, verbose):
        """ By default, libConviqt results returns a signal that conforms to
        TOD = (1 + epsilon) / 2 * intensity + (1 - epsilon) / 2 * polarization.

        When calibrate = True, we rescale the TOD to
        TOD = intensity + (1 - epsilon) / (1 + epsilon) * polarization
        """
        if not self.calibrate:
            return
        timer = Timer()
        timer.start()
        offset = 0
        for obs in data.obs:
            tod = obs["tod"]
            if det not in tod.local_dets:
                continue
            focalplane = obs["focalplane"]
            epsilon = self._get_epsilon(focalplane, det)
            nsample = tod.local_samples[1]
            convolved_data[offset : offset + nsample] *= 2 / (1 + epsilon)
            offset += nsample
        if verbose:
            timer.report_clear("calibrate detector {}".format(det))
        return

    def cache(self, data, det, convolved_data, verbose):
        """ Inject the convolved data into the TOD cache.
        """
        timer = Timer()
        timer.start()
        offset = 0
        for obs in data.obs:
            tod = obs["tod"]
            if det not in tod.local_dets:
                continue
            nsample = tod.local_samples[1]
            cachename = "{}_{}".format(self._out, det)
            if not tod.cache.exists(cachename):
                tod.cache.create(cachename, np.float64, (nsample,))
            ref = tod.cache.reference(cachename)
            ref[:] += convolved_data[offset : offset + nsample]
            offset += nsample
        if verbose:
            timer.report_clear("cache detector {}".format(det))
        return
