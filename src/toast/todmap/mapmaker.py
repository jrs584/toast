from collections import OrderedDict
import os
import sys

import numpy as np
import scipy.signal

from toast import Operator
from toast.mpi import MPI

from toast.timing import function_timer, Timer
from toast.utils import Logger, Environment
from .sim_det_map import OpSimScan
from .todmap_math import OpAccumDiag, OpLocalPixels, OpScanScale, OpScanMask
from ..tod import OpCacheClear, OpCacheCopy, OpCacheInit, OpFlagsApply
from ..map import covariance_apply, covariance_invert, DistPixels
from toast.pipeline_tools.pointing import get_submaps


class TOASTMatrix:
    def apply(self, vector, inplace=False):
        """ Every TOASTMatrix can apply itself to a distributed vectors
        of signal, map or template offsets as is appropriate.
        """
        raise NotImplementedError("Virtual apply not implemented in derived class")

    def applyTranspose(self, vector, inplace=False):
        """ Every TOASTMatrix can apply itself to a distributed vectors
        of signal, map or template offsets as is appropriate.
        """
        raise NotImplementedError(
            "Virtual applyTranspose not implemented in derived class"
        )


class TOASTVector:
    def dot(self, other):
        raise NotImplementedError("Virtual dot not implemented in derived class")


class UnitMatrix(TOASTMatrix):
    def apply(self, vector, inplace=False):
        if inplace:
            outvec = vector
        else:
            outvec = vector.copy()
        return outvec


class TODTemplate:
    """ Parent class for all templates that can be registered with
    TemplateMatrix
    """

    name = None
    namplitude = 0
    comm = None

    def __init___(self, *args, **kwargs):
        raise NotImplementedError("Derived class must implement __init__()")

    def add_to_signal(self, signal, amplitudes):
        """ signal += F.a
        """
        raise NotImplementedError("Derived class must implement add_to_signal()")

    def project_signal(self, signal, amplitudes):
        """ a += F^T.signal
        """
        raise NotImplementedError("Derived class must implement project_signal()")

    def add_prior(self, amplitudes_in, amplitudes_out):
        """ a' += C_a^{-1}.a
        """
        # Not all TODTemplates implement the prior
        return

    def apply_precond(self, amplitudes_in, amplitudes_out):
        """ a' = M^{-1}.a
        """
        raise NotImplementedError("Derived class must implement apply_precond()")


class SubharmonicTemplate(TODTemplate):
    """ This class represents sub-harmonic noise fluctuations.

    Sub-harmonic means that the characteristic frequency of the noise
    modes is lower than 1/T where T is the length of the interval
    being fitted.
    """

    name = "subharmonic"
    _last_nsamp = None
    _last_templates = None

    def __init__(
        self,
        data,
        detweights,
        order=1,
        intervals=None,
        common_flags=None,
        common_flag_mask=1,
        flags=None,
        flag_mask=1,
    ):
        self.data = data
        self.detweights = detweights
        self.order = order
        self.intervals = intervals
        self.common_flags = common_flags
        self.common_flag_mask = common_flag_mask
        self.flags = flags
        self.flag_mask = flag_mask
        self.get_steps_and_preconditioner()

    def get_steps_and_preconditioner(self):
        """ Assign each template an amplitude
        """
        self.templates = []
        self.slices = []
        self.preconditioners = []
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            common_flags = tod.local_common_flags(self.common_flags)
            common_flags = (common_flags & self.common_flag_mask) != 0
            if self.intervals and self.intervals in obs:
                intervals = obs[self.intervals]
            else:
                intervals = None
            local_intervals = tod.local_intervals(intervals)
            slices = {}  # this observation
            preconditioners = {}  # this observation
            for ival in local_intervals:
                todslice = slice(ival.first, ival.last + 1)
                for idet, det in enumerate(tod.local_dets):
                    ind = slice(self.namplitude, self.namplitude + self.order + 1)
                    self.templates.append([ind, iobs, det, todslice])
                    self.namplitude += self.order + 1
                    preconditioner = self._get_preconditioner(
                        det, tod, todslice, common_flags, self.detweights[iobs][det]
                    )
                    if det not in preconditioners:
                        preconditioners[det] = []
                        slices[det] = []
                    preconditioners[det].append(preconditioner)
                    slices[det].append(ind)
            self.slices.append(slices)
            self.preconditioners.append(preconditioners)
        return

    def _get_preconditioner(self, det, tod, todslice, common_flags, detweight):
        """ Calculate the preconditioner for the given interval and detector
        """
        flags = tod.local_flags(det, self.flags)[todslice]
        flags = (flags & self.flag_mask) != 0
        flags[common_flags[todslice]] = True
        good = np.logical_not(flags)
        norder = self.order + 1
        preconditioner = np.zeros([norder, norder])
        templates = self._get_templates(todslice)
        for row in range(norder):
            for col in range(row, norder):
                preconditioner[row, col] = np.dot(
                    templates[row][good], templates[col][good]
                )
                preconditioner[row, col] *= detweight
                if row != col:
                    preconditioner[col, row] = preconditioner[row, col]
        preconditioner = np.linalg.inv(preconditioner)
        return preconditioner

    def add_to_signal(self, signal, amplitudes):
        subharmonic_amplitudes = amplitudes[self.name]
        for ibase, (ind, iobs, det, todslice) in enumerate(self.templates):
            templates = self._get_templates(todslice)
            amps = subharmonic_amplitudes[ind]
            for template, amplitude in zip(templates, amps):
                signal[iobs, det, todslice] += template * amplitude
        return

    def _get_templates(self, todslice):
        """ Develop hierarchy of subharmonic modes matching the given length

        The basis functions are (orthogonal) Legendre polynomials
        """
        nsamp = todslice.stop - todslice.start
        if nsamp != self._last_nsamp:
            templates = np.zeros([self.order + 1, nsamp])
            r = np.linspace(-1, 1, nsamp)
            for order in range(self.order + 1):
                if order == 0:
                    templates[order] = 1
                elif order == 1:
                    templates[order] = r
                else:
                    templates[order] = (
                        (2 * order - 1) * r * templates[order - 1]
                        - (order - 1) * templates[order - 2]
                    ) / order
            self._last_nsamp = nsamp
            self._last_templates = templates
        return self._last_templates

    def project_signal(self, signal, amplitudes):
        subharmonic_amplitudes = amplitudes[self.name]
        for ibase, (ind, iobs, det, todslice) in enumerate(self.templates):
            templates = self._get_templates(todslice)
            amps = subharmonic_amplitudes[ind]
            for order, template in enumerate(templates):
                amps[order] = np.dot(signal[iobs, det, todslice], template)
        pass

    def apply_precond(self, amplitudes_in, amplitudes_out):
        """ Standard diagonal preconditioner accounting for the fact that
        the templates are not orthogonal in the presence of flagging and masking
        """
        subharmonic_amplitudes_in = amplitudes_in[self.name]
        subharmonic_amplitudes_out = amplitudes_out[self.name]
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            for det in tod.local_dets:
                slices = self.slices[iobs][det]
                preconditioners = self.preconditioners[iobs][det]
                for ind, preconditioner in zip(slices, preconditioners):
                    subharmonic_amplitudes_out[ind] = np.dot(
                        preconditioner, subharmonic_amplitudes_in[ind]
                    )
        return


class OffsetTemplate(TODTemplate):
    """ This class represents noise fluctuations as a step function
    """

    name = "offset"

    def __init__(self, data, step_length=1000000, intervals=None):
        self.data = data
        self.step_length = step_length
        self.intervals = intervals
        self.get_steps()
        self.get_filters()

    def get_filters(self):
        """ Compute and store the filter and associated preconditioner
        for every detector and every observation
        """
        log = Logger.get()
        self.filters = []  # all observations
        self.preconditioners = []  # all observations
        for iobs, obs in enumerate(self.data.obs):
            if "noise" not in obs:
                # If the observations do not include noise PSD:s, we
                # we cannot build filters.
                if len(self.filters) > 0:
                    log.warning(
                        'Observation "{}" does not have noise information'
                        "".format(obs["name"])
                    )
                continue
            tod = obs["tod"]
            # Determine the binning for the noise prior
            times = tod.local_times()
            dtime = np.amin(np.diff(times))
            fsample = 1 / dtime
            obstime = times[-1] - times[0]
            tbase = self.step_length
            fbase = 1 / tbase
            powmin = np.floor(np.log10(1 / obstime)) - 1
            powmax = min(np.ceil(np.log10(1 / tbase)) + 2, fsample)
            freq = np.logspace(powmin, powmax, 1000)
            # Now build the filter for each detector
            noise = obs["noise"]
            noisefilters = {}  # this observation
            preconditioners = {}  # this observation
            for det in tod.local_dets:
                psdfreq = noise.freq(det)
                psd = noise.psd(det)
                # Remove the white noise component from the PSD
                psd = psd.copy()
                psd -= np.amin(psd[psdfreq > 1.0])
                psd[psd < 1e-30] = 1e-30
                offset_psd = self._get_offset_psd(psdfreq, psd, freq)
                # Store real space filters for every interval and every detector.
                noisefilters[det], preconditioners[
                    det
                ] = self._get_noisefilter_and_preconditioner(
                    freq, offset_psd, self.offset_slices[iobs][det]
                )
            self.filters.append(noisefilters)
            self.preconditioners.append(preconditioners)
        return

    def _get_offset_psd(self, psdfreq, psd, freq):
        # The calculation of `offset_psd` is from Keihänen, E. et al:
        # "Making CMB temperature and polarization maps with Madam",
        # A&A 510:A57, 2010
        logfreq = np.log(psdfreq)
        logpsd = np.log(psd)

        def interpolate_psd(x):
            result = np.zeros(x.size)
            good = np.abs(x) > 1e-10
            logx = np.log(np.abs(x[good]))
            logresult = np.interp(logx, logfreq, logpsd)
            result[good] = np.exp(logresult)
            return result

        def g(x):
            bad = np.abs(x) < 1e-10
            good = np.logical_not(bad)
            arg = np.pi * x[good]
            result = bad.astype(np.float64)
            result[good] = (np.sin(arg) / arg) ** 2
            return result

        tbase = self.step_length
        fbase = 1 / tbase
        offset_psd = interpolate_psd(freq) * g(freq * tbase)
        for m in range(1, 2):
            offset_psd += interpolate_psd(freq + m * fbase) * g(freq * tbase + m)
            offset_psd += interpolate_psd(freq - m * fbase) * g(freq * tbase - m)
        offset_psd *= fbase
        return offset_psd

    def _get_noisefilter_and_preconditioner(self, freq, offset_psd, offset_slices):
        logfreq = np.log(freq)
        logpsd = np.log(offset_psd)
        logfilter = np.log(1 / offset_psd)

        def interpolate(x, psd):
            result = np.zeros(x.size)
            good = np.abs(x) > 1e-10
            logx = np.log(np.abs(x[good]))
            logresult = np.interp(logx, logfreq, psd)
            result[good] = np.exp(logresult)
            return result

        def truncate(noisefilter, lim=1e-4):
            icenter = noisefilter.size // 2
            ind = np.abs(noisefilter[:icenter]) > np.abs(noisefilter[0]) * lim
            icut = np.argwhere(ind)[-1][0]
            if icut % 2 == 0:
                icut += 1
            noisefilter = np.roll(noisefilter, icenter)
            noisefilter = noisefilter[icenter - icut : icenter + icut + 1]
            return noisefilter

        noisefilters = []
        preconditioners = []
        for offset_slice in offset_slices:
            nstep = offset_slice.stop - offset_slice.start
            nstep = nstep * 2 + 1
            filterfreq = np.fft.rfftfreq(nstep, self.step_length)
            preconditioner = truncate(np.fft.irfft(interpolate(filterfreq, logpsd)))
            noisefilter = truncate(np.fft.irfft(interpolate(filterfreq, logfilter)))
            noisefilters.append(noisefilter)
            preconditioners.append(preconditioner)
        return noisefilters, preconditioners

    def get_steps(self):
        """ Divide each interval into offset steps
        """
        self.offset_templates = []
        self.offset_slices = []  # slices in all observations
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            if self.intervals and self.intervals in obs:
                intervals = obs[self.intervals]
            else:
                intervals = None
            local_intervals = tod.local_intervals(intervals)
            times = tod.local_times()
            offset_slices = {}  # slices in this observation
            for ival in local_intervals:
                length = times[ival.last] - times[ival.first]
                nbase = int(np.ceil(length / self.step_length))
                # Divide the interval into steps, allowing for irregular sampling
                todslices = []
                start_times = np.arange(nbase) * self.step_length + ival.start
                start_indices = np.searchsorted(times, start_times)
                stop_indices = np.hstack([start_indices[1:], [ival.last]])
                todslices = []
                for istart, istop in zip(start_indices, stop_indices):
                    todslices.append(slice(istart, istop + 1))
                for idet, det in enumerate(tod.local_dets):
                    istart = self.namplitude
                    for todslice in todslices:
                        self.offset_templates.append(
                            [self.namplitude, iobs, det, todslice]
                        )
                        self.namplitude += 1
                    # Keep a record of ranges of offsets that correspond
                    # to one detector and one interval.
                    # This is the domain we apply the noise filter in.
                    if det not in offset_slices:
                        offset_slices[det] = []
                    offset_slices[det].append(slice(istart, self.namplitude))
            self.offset_slices.append(offset_slices)
        return

    def add_to_signal(self, signal, amplitudes):
        offset_amplitudes = amplitudes[self.name]
        for ibase, (itemplate, iobs, det, todslice) in enumerate(self.offset_templates):
            signal[iobs, det, todslice] += offset_amplitudes[itemplate]
        return

    def project_signal(self, signal, amplitudes):
        offset_amplitudes = amplitudes[self.name]
        for ibase, (itemplate, iobs, det, todslice) in enumerate(self.offset_templates):
            offset_amplitudes[itemplate] += np.sum(signal[iobs, det, todslice])
        return

    def add_prior(self, amplitudes_in, amplitudes_out):
        offset_amplitudes_in = amplitudes_in[self.name]
        offset_amplitudes_out = amplitudes_out[self.name]
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            for det in tod.local_dets:
                slices = self.offset_slices[iobs][det]
                filters = self.filters[iobs][det]
                for offsetslice, noisefilter in zip(slices, filters):
                    amps_in = offset_amplitudes_in[offsetslice]
                    # scipy.signal.convolve will use either `convolve` or `fftconvolve`
                    # depending on the size of the inputs
                    amps_out = scipy.signal.convolve(amps_in, noisefilter, mode="same")
                    offset_amplitudes_out[offsetslice] = amps_out
        return

    def apply_precond(self, amplitudes_in, amplitudes_out):
        offset_amplitudes_in = amplitudes_in[self.name]
        offset_amplitudes_out = amplitudes_out[self.name]
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            for det in tod.local_dets:
                slices = self.offset_slices[iobs][det]
                preconditioners = self.preconditioners[iobs][det]
                for offsetslice, preconditioner in zip(slices, preconditioners):
                    amps_in = offset_amplitudes_in[offsetslice]
                    # scipy.signal.convolve will use either `convolve` or `fftconvolve`
                    # depending on the size of the inputs
                    amps_out = scipy.signal.convolve(
                        amps_in, preconditioner, mode="same"
                    )
                    offset_amplitudes_out[offsetslice] = amps_out
        return


class TemplateMatrix(TOASTMatrix):

    templates = []

    def __init__(self, data, comm, templates=None):
        """ Initialize the template matrix with a given baseline length
        """
        self.data = data
        self.comm = comm
        for template in templates:
            self.register_template(template)
        return

    def register_template(self, template):
        """ Add template to the list of templates to fit
        """
        self.templates.append(template)

    def __del__(self):
        # Destroy any temporary objects placed in the TOD caches
        for obs in self.data.obs:
            tod = obs["tod"]
            tod.cache.clear("temporary.*")
        return

    def apply(self, amplitudes):
        """ Compute and return y = F.a
        """
        new_signal = self.zero_signal()
        for template in self.templates:
            template.add_to_signal(new_signal, amplitudes)
        return new_signal

    def applyTranspose(self, signal):
        """ Compute and return a = F^T.y
        """
        new_amplitudes = self.zero_amplitudes()
        for template in self.templates:
            template.project_signal(signal, new_amplitudes)
        return new_amplitudes

    def add_prior(self, amplitudes, new_amplitudes):
        """ Compute a' += C_a^{-1}.a
        """
        for template in self.templates:
            template.add_prior(amplitudes, new_amplitudes)
        return

    def apply_precond(self, amplitudes):
        """ Compute a' = M^{-1}.a
        """
        new_amplitudes = self.zero_amplitudes()
        for template in self.templates:
            template.apply_precond(amplitudes, new_amplitudes)
        return new_amplitudes

    def zero_amplitudes(self):
        """ Return a null amplitudes object
        """
        new_amplitudes = TemplateAmplitudes(self.templates, self.comm)
        return new_amplitudes

    def zero_signal(self):
        """ Return a distributed vector of signal set to zero.

        The zero signal object will use the same TOD objects but different cache prefix
        """
        new_signal = Signal(self.data, name="temporary", init_val=0)
        return new_signal

    def clean_signal(self, signal, amplitudes, in_place=True):
        """ Clean the given distributed signal vector by subtracting
        the templates multiplied by the given amplitudes.
        """
        if in_place:
            outsignal = signal
        else:
            outsignal = signal.copy()
        template_tod = self.apply(amplitudes)
        outsignal -= template_tod
        return outsignal


class TemplateAmplitudes(TOASTVector):
    """ TemplateAmplitudes objects hold local and shared template amplitudes
    """

    def __init__(self, templates, comm):
        self.comm = comm
        self.amplitudes = OrderedDict()
        self.comms = OrderedDict()
        for template in templates:
            self.amplitudes[template.name] = np.zeros(template.namplitude)
            self.comms[template.name] = template.comm
        return

    def __str__(self):
        result = "template amplitudes:\n"
        for name, values in self.amplitudes.items():
            result += '"{}" : {}\n'.format(name, values)
        return result

    def dot(self, other):
        """ Compute the dot product between the two amplitude vectors
        """
        total = 0
        for name, values in self.amplitudes.items():
            dp = np.dot(values, other.amplitudes[name])
            comm = self.comms[name]
            if comm is not None:
                dp = comm.reduce(dp, op=MPI.SUM)
                if comm.rank != 0:
                    dp = 0
            total += dp
        if self.comm is not None:
            total = self.comm.allreduce(total, op=MPI.SUM)
        return total

    def __getitem__(self, key):
        return self.amplitudes[key]

    def __setitem__(self, key, value):
        self.amplitudes[name][:] = value
        return

    def copy(self):
        new_amplitudes = TemplateAmplitudes([], self.comm)
        for name, values in self.amplitudes.items():
            new_amplitudes.amplitudes[name] = self.amplitudes[name].copy()
            new_amplitudes.comms[name] = self.comms[name]
        return new_amplitudes

    def __iadd__(self, other):
        """ Add the provided amplitudes to this one
        """
        if isinstance(other, TemplateAmplitudes):
            for name, values in self.amplitudes.items():
                values += other.amplitudes[name]
        else:
            for name, values in self.amplitudes.items():
                values += other
        return self

    def __isub__(self, other):
        """ Subtract the provided amplitudes from this one
        """
        if isinstance(other, TemplateAmplitudes):
            for name, values in self.amplitudes.items():
                values -= other.amplitudes[name]
        else:
            for name, values in self.amplitudes.items():
                values -= other
        return self

    def __imul__(self, other):
        """ Scale the amplitudes
        """
        for name, values in self.amplitudes.items():
            values *= other
        return self

    def __itruediv__(self, other):
        """ Divide the amplitudes
        """
        for name, values in self.amplitudes.items():
            values /= other
        return self


class TemplateCovariance(TOASTMatrix):
    def __init__(self):
        pass


class ProjectionMatrix(TOASTMatrix):
    """ Projection matrix:
            Z = I - P (P^T N^{-1} P)^{-1} P^T N^{-1}
              = I - P B,
        where
             `P` is the pointing matrix
             `N` is the noise matrix and
             `B` is the binning operator
    """

    def __init__(
        self,
        data,
        comm,
        detweights,
        npix,
        nnz,
        subnpix,
        localsm,
        white_noise_cov_matrix,
        common_flag_mask=1,
        flag_mask=1,
    ):
        self.data = data
        self.comm = comm
        self.detweights = detweights
        self.dist_map = DistPixels(
            comm=self.comm,
            size=npix,
            nnz=nnz,
            dtype=np.float64,
            submap=subnpix,
            local=localsm,
        )
        self.white_noise_cov_matrix = white_noise_cov_matrix
        self.common_flag_mask = common_flag_mask
        self.flag_mask = flag_mask

    def __del__(self):
        for obs in self.data.obs:
            tod = obs["tod"]
            tod.cache.clear("temporary3.*")
        return

    def apply(self, signal):
        """ Return Z.y
        """
        self.bin_map(signal.name)
        new_signal = signal.copy()
        name = "temporary3"
        scanned_signal = Signal(self.data, name=name, init_val=0)
        self.scan_map(name)
        new_signal -= scanned_signal
        return new_signal

    def bin_map(self, name):
        if self.dist_map.data is not None:
            self.dist_map.data.fill(0.0)
        # FIXME: OpAccumDiag should support separate detweights for each observation
        build_dist_map = OpAccumDiag(
            zmap=self.dist_map,
            name=name,
            detweights=self.detweights[0],
            common_flag_mask=self.common_flag_mask,
            flag_mask=self.flag_mask,
        )
        build_dist_map.exec(self.data)
        self.dist_map.allreduce()
        covariance_apply(self.white_noise_cov_matrix, self.dist_map)
        return

    def scan_map(self, name):
        scansim = OpSimScan(distmap=self.dist_map, out=name)
        scansim.exec(self.data)
        return


class NoiseMatrix(TOASTMatrix):
    def __init__(
        self, comm, detweights, weightmap=None, common_flag_mask=1, flag_mask=1
    ):
        self.comm = comm
        self.detweights = detweights
        self.weightmap = weightmap
        self.common_flag_mask = common_flag_mask
        self.flag_mask = flag_mask

    def apply(self, signal, in_place=False):
        """ Multiplies the signal with N^{-1}.

        Note that the quality flags cause the corresponding diagonal
        elements of N^{-1} to be zero.
        """
        if in_place:
            new_signal = signal
        else:
            new_signal = signal.copy()
        for iobs, detweights in enumerate(self.detweights):
            for det, detweight in detweights.items():
                new_signal[iobs, det, :] *= detweight
        # Set flagged samples to zero
        new_signal.apply_flags(self.common_flag_mask, self.flag_mask)
        # Scale the signal with the weight map
        new_signal.apply_weightmap(self.weightmap)
        return new_signal

    def applyTranspose(self, signal):
        # Symmetric matrix
        return self.apply(signal)


class PointingMatrix(TOASTMatrix):
    def __init__(self):
        pass


class Signal(TOASTVector):
    def __init__(self, data, name=None, init_val=None):
        self.data = data
        self.name = name
        if init_val is not None:
            cacheinit = OpCacheInit(name=self.name, init_val=init_val)
            cacheinit.exec(data)
        return

    def apply_flags(self, common_flag_mask, flag_mask):
        """ Set the signal at flagged samples to zero
        """
        flags_apply = OpFlagsApply(
            name=self.name, common_flag_mask=common_flag_mask, flag_mask=flag_mask
        )
        flags_apply.exec(self.data)
        return

    def apply_weightmap(self, weightmap):
        """ Scale the signal with the provided weight map
        """
        if weightmap is None:
            return
        scanscale = OpScanScale(distmap=weightmap, name=self.name)
        scanscale.exec(self.data)
        return

    def copy(self):
        """ Return a new Signal object with independent copies of the
        signal vectors.
        """
        new_name = "temporary2"
        copysignal = OpCacheCopy(self.name, new_name, force=True)
        copysignal.exec(self.data)
        new_signal = Signal(self.data, name=new_name)
        return new_signal

    def __getitem__(self, key):
        """ Return a reference to a slice of TOD cache
        """
        iobs, det, todslice = key
        tod = self.data.obs[iobs]["tod"]
        return tod.local_signal(det, self.name)[todslice]

    def __setitem__(self, key, value):
        """ Set slice of TOD cache
        """
        iobs, det, todslice = key
        tod = self.data.obs[iobs]["tod"]
        tod.local_signal(det, self.name)[todslice] = value
        return

    def __iadd__(self, other):
        """ Add the provided Signal object to this one
        """
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            for det in tod.local_dets:
                if isinstance(other, Signal):
                    self[iobs, det, :] += other[iobs, det, :]
                else:
                    self[iobs, det, :] += other
        return self

    def __isub__(self, other):
        """ Subtract the provided Signal object from this one
        """
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            for det in tod.local_dets:
                if isinstance(other, Signal):
                    self[iobs, det, :] -= other[iobs, det, :]
                else:
                    self[iobs, det, :] -= other
        return self

    def __imul__(self, other):
        """ Scale the signal
        """
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            for det in tod.local_dets:
                self[iobs, det, :] *= other
        return self

    def __itruediv__(self, other):
        """ Divide the signal
        """
        for iobs, obs in enumerate(self.data.obs):
            tod = obs["tod"]
            for det in tod.local_dets:
                self[iobs, det, :] /= other
        return self


class PCGSolver:
    """ Solves `x` in A.x = b
    """

    def __init__(
        self,
        comm,
        templates,
        noise,
        projection,
        signal,
        niter_min=3,
        niter_max=100,
        convergence_limit=1e-12,
    ):
        self.comm = comm
        if comm is None:
            self.rank = 0
        else:
            self.rank = comm.rank
        self.templates = templates
        self.noise = noise
        self.projection = projection
        self.signal = signal
        self.niter_min = niter_min
        self.niter_max = niter_max
        self.convergence_limit = convergence_limit

        self.rhs = self.templates.applyTranspose(
            self.noise.apply(self.projection.apply(self.signal))
        )
        # print("RHS: {}".format(self.rhs))  # DEBUG
        return

    def apply_lhs(self, amplitudes):
        """ Return A.x
        """
        new_amplitudes = self.templates.applyTranspose(
            self.noise.apply(self.projection.apply(self.templates.apply(amplitudes)))
        )
        self.templates.add_prior(amplitudes, new_amplitudes)
        return new_amplitudes

    def solve(self):
        """ Standard issue PCG solution of A.x = b

        Returns:
            x : the least squares solution
        """
        log = Logger.get()
        timer0 = Timer()
        timer0.start()
        timer = Timer()
        timer.start()
        # Initial guess is zero amplitudes
        guess = self.templates.zero_amplitudes()
        # print("guess:", guess)  # DEBUG
        # print("RHS:", self.rhs)  # DEBUG
        residual = self.rhs.copy()
        # print("residual(1):", residual)  # DEBUG
        residual -= self.apply_lhs(guess)
        # print("residual(2):", residual)  # DEBUG
        precond_residual = self.templates.apply_precond(residual)
        proposal = precond_residual.copy()
        sqsum = precond_residual.dot(residual)
        init_sqsum, best_sqsum, last_best = sqsum, sqsum, sqsum
        if self.rank == 0:
            log.info("Initial residual: {}".format(init_sqsum))
        # Iterate to convergence
        for iiter in range(self.niter_max):
            if not np.isfinite(sqsum):
                raise RuntimeError("Residual is not finite")
            alpha = sqsum
            alpha /= proposal.dot(self.apply_lhs(proposal))
            alpha_proposal = proposal.copy()
            alpha_proposal *= alpha
            guess += alpha_proposal
            residual -= self.apply_lhs(alpha_proposal)
            del alpha_proposal
            # Prepare for next iteration
            precond_residual = self.templates.apply_precond(residual)
            beta = 1 / sqsum
            # Check for convergence
            sqsum = precond_residual.dot(residual)
            if self.rank == 0:
                timer.report_clear(
                    "Iter = {:4} relative residual: {:12.4e}".format(
                        iiter, sqsum / init_sqsum
                    )
                )
            if sqsum < init_sqsum * self.convergence_limit:
                if self.rank == 0:
                    timer0.report_clear(
                        "PCG converged after {} iterations".format(iiter)
                    )
                break
            best_sqsum = min(sqsum, best_sqsum)
            if iiter % 10 == 0 and iiter >= self.niter_min:
                if last_best < best_sqsum * 2:
                    if self.rank == 0:
                        timer0.report_clear(
                            "PCG stalled after {} iterations".format(iiter)
                        )
                    break
                last_best = best_sqsum
            # Select the next direction
            beta *= sqsum
            proposal *= beta
            proposal += precond_residual
        log.info("{} : Solution: {}".format(self.rank, guess))  # DEBUG
        return guess


class OpMapMaker(Operator):

    # Choose one bit in the quality flags for storing processing mask
    maskbit = 2 ** 7
    # In addition to the mask, we may down-weight the sky by pixel
    weightmap = None

    def __init__(
        self,
        nside=64,
        nnz=3,
        name=None,
        outdir="out",
        outprefix="",
        write_hits=True,
        zip_maps=False,
        write_wcov_inv=True,
        write_wcov=True,
        write_binned=True,
        write_destriped=True,
        baseline_length=100000,
        maskfile=None,
        weightmapfile=None,
        common_flag_mask=1,
        flag_mask=1,
        intervals="intervals",
        subharmonic_order=None,
    ):
        self.nside = nside
        self.npix = 12 * self.nside ** 2
        self.name = name
        self.subnside = min(16, self.nside)
        self.subnpix = 12 * self.subnside ** 2
        self.nnz = nnz
        self.ncov = self.nnz * (self.nnz + 1) // 2
        self.outdir = outdir
        self.outprefix = outprefix
        self.write_hits = write_hits
        self.zip_maps = zip_maps
        self.write_wcov_inv = write_wcov_inv
        self.write_wcov = write_wcov
        self.write_binned = write_binned
        self.write_destriped = write_destriped
        self.baseline_length = baseline_length
        self.maskfile = maskfile
        self.weightmapfile = weightmapfile
        self.common_flag_mask = common_flag_mask
        self.flag_mask = flag_mask
        self.intervals = intervals
        self.subharmonic_order = subharmonic_order

    def load_mask(self, data):
        """ Load processing mask and generate appropriate flag bits
        """
        if self.maskfile is None:
            return
        log = Logger.get()
        timer = Timer()
        timer.start()
        if self.rank == 0 and not os.path.isfile(self.maskfile):
            raise RuntimeError(
                "Processing mask does not exist: {}".format(self.maskfile)
            )
        distmap = DistPixels(
            comm=self.comm,
            size=self.npix,
            nnz=1,
            dtype=np.float32,
            submap=self.subnpix,
            local=self.localsm,
        )
        distmap.read_healpix_fits(self.maskfile)
        if self.rank == 0:
            timer.report_clear("Read processing mask from {}".format(self.maskfile))

        scanmask = OpScanMask(distmap=distmap, flagmask=self.maskbit)
        scanmask.exec(data)

        if self.rank == 0:
            timer.report_clear("Apply processing mask")

        return

    def load_weightmap(self, data):
        """ Load weight map
        """
        if self.weightmapfile is None:
            return
        log = Logger.get()
        timer = Timer()
        timer.start()
        if self.rank == 0 and not os.path.isfile(self.weightmapfile):
            raise RuntimeError(
                "Weight map does not exist: {}".format(self.weightmapfile)
            )
        self.weightmap = DistPixels(
            comm=self.comm,
            size=self.npix,
            nnz=1,
            dtype=np.float32,
            submap=self.subnpix,
            local=self.localsm,
        )
        self.weightmap.read_healpix_fits(self.weightmapfile)
        if self.rank == 0:
            timer.report_clear("Read weight map from {}".format(self.weightmapfile))
        return

    def exec(self, data):
        log = Logger.get()
        timer = Timer()
        timer.start()
        # Initialize objects
        self.comm = data.comm.comm_world
        if self.comm is None:
            self.rank = 0
        else:
            self.rank = self.comm.rank
        self.get_detweights(data)
        self.initialize_binning(data)
        if self.write_binned:
            self.bin_map(data, "binned")
        self.load_mask(data)
        self.load_weightmap(data)
        if self.rank == 0:
            timer.report_clear("Initialize mapmaking")

        # Solve template amplitudes

        precond = UnitMatrix()
        templatelist = []
        if self.baseline_length is not None:
            templatelist.append(
                OffsetTemplate(
                    data, step_length=self.baseline_length, intervals=self.intervals
                )
            )
        if self.subharmonic_order is not None:
            templatelist.append(
                SubharmonicTemplate(
                    data,
                    self.detweights,
                    order=self.subharmonic_order,
                    intervals=self.intervals,
                    common_flag_mask=self.common_flag_mask,
                    flag_mask=(self.flag_mask | self.maskbit),
                )
            )

        if len(templatelist) == 0:
            if self.rank == 0:
                log.info("No templates to fit, no destriping done.")
            return

        templates = TemplateMatrix(data, self.comm, templatelist)
        noise = NoiseMatrix(
            self.comm,
            self.detweights,
            self.weightmap,
            common_flag_mask=self.common_flag_mask,
            flag_mask=(self.flag_mask | self.maskbit),
        )
        if self.rank == 0:
            timer.report_clear("Initialize templates")

        projection = ProjectionMatrix(
            data,
            self.comm,
            self.detweights,
            self.npix,
            self.nnz,
            self.subnpix,
            self.localsm,
            self.white_noise_cov_matrix,
            common_flag_mask=self.common_flag_mask,
            # Do not add maskbit here since it is not included in the white noise matrices
            flag_mask=self.flag_mask,
        )
        if self.rank == 0:
            timer.report_clear("Initialize projection matrix")
        # projection = UnitMatrix()  # DEBUG
        signal = Signal(data, name=self.name)
        # DEBUG begin
        # signal += 100
        # DEBUG end

        solver = PCGSolver(self.comm, templates, noise, projection, signal)
        if self.rank == 0:
            timer.report_clear("Initialize PCG solver")
        amplitudes = solver.solve()
        if self.rank == 0:
            timer.report_clear("Solve amplitudes")

        # Clean TOD
        templates.clean_signal(signal, amplitudes)
        if self.rank == 0:
            timer.report_clear("Clean TOD")

        if self.write_destriped:
            self.bin_map(data, "destriped")
            if self.rank == 0:
                timer.report_clear("Write destriped map")

        return

    def bin_map(self, data, suffix):
        log = Logger.get()
        timer = Timer()

        dist_map = DistPixels(
            comm=self.comm,
            size=self.npix,
            nnz=self.nnz,
            dtype=np.float64,
            submap=self.subnpix,
            local=self.localsm,
        )
        if dist_map.data is not None:
            dist_map.data.fill(0.0)
        # FIXME: OpAccumDiag should support separate detweights for each observation
        build_dist_map = OpAccumDiag(
            zmap=dist_map,
            name=self.name,
            detweights=self.detweights[0],
            common_flag_mask=self.common_flag_mask,
            flag_mask=self.flag_mask,
        )
        build_dist_map.exec(data)
        dist_map.allreduce()
        if self.rank == 0:
            timer.report_clear("  Build noise-weighted map")

        covariance_apply(self.white_noise_cov_matrix, dist_map)
        if self.rank == 0:
            timer.report_clear("  Apply noise covariance")

        fname = os.path.join(self.outdir, self.outprefix + suffix + ".fits")
        if self.zip_maps:
            fname += ".gz"
        dist_map.write_healpix_fits(fname)
        if self.rank == 0:
            timer.report_clear("  Write map to {}".format(fname))

        return

    def get_detweights(self, data):
        """ Each observation will have its own detweight dictionary
        """
        self.detweights = []
        for obs in data.obs:
            tod = obs["tod"]
            try:
                noise = obs["noise"]
            except:
                noise = None
            detweights = {}
            for det in tod.local_dets:
                if noise is None:
                    noisevar = 1
                else:
                    # Determine an approximate white noise level,
                    # accounting for the fact that the PSD may have a
                    # transfer function roll-off near Nyquist
                    freq = noise.freq(det)
                    psd = noise.psd(det)
                    rate = noise.rate(det)
                    ind = np.logical_and(freq > rate * 0.2, freq < rate * 0.4)
                    noisevar = np.median(psd[ind]) * rate
                detweights[det] = 1 / noisevar
            self.detweights.append(detweights)
        return

    def initialize_binning(self, data):
        log = Logger.get()
        timer = Timer()
        timer.start()

        if self.rank == 0:
            os.makedirs(self.outdir, exist_ok=True)

        # get locally hit pixels
        lc = OpLocalPixels()
        localpix = lc.exec(data)
        if localpix is None:
            raise RuntimeError(
                "Process {} has no hit pixels. Perhaps there are fewer "
                "detectors than processes in the group?".format(self.rank)
            )

        # find the locally hit submaps.
        self.localsm = np.unique(np.floor_divide(localpix, self.subnpix))

        if self.rank == 0:
            timer.report_clear("Identify local submaps")

        self.white_noise_cov_matrix = DistPixels(
            comm=self.comm,
            size=self.npix,
            nnz=self.ncov,
            dtype=np.float64,
            submap=self.subnpix,
            local=self.localsm,
        )
        if self.white_noise_cov_matrix.data is not None:
            self.white_noise_cov_matrix.data.fill(0.0)

        hits = DistPixels(
            comm=self.comm,
            size=self.npix,
            nnz=1,
            dtype=np.int64,
            submap=self.subnpix,
            local=self.localsm,
        )
        if hits.data is not None:
            hits.data.fill(0)

        # compute the hits and covariance once, since the pointing and noise
        # weights are fixed.
        # FIXME: OpAccumDiag should support separate weights for each observation

        build_wcov = OpAccumDiag(
            detweights=self.detweights[0],
            invnpp=self.white_noise_cov_matrix,
            hits=hits,
            common_flag_mask=self.common_flag_mask,
            flag_mask=self.flag_mask,
        )
        build_wcov.exec(data)

        if self.rank == 0:
            timer.report_clear("Accumulate N_pp'^1")

        self.white_noise_cov_matrix.allreduce()

        if self.rank == 0:
            timer.report_clear("All reduce N_pp'^1")

        if self.write_hits:
            hits.allreduce()
            fname = os.path.join(self.outdir, self.outprefix + "hits.fits")
            if self.zip_maps:
                fname += ".gz"
            hits.write_healpix_fits(fname)
            if self.rank == 0:
                log.info("Wrote hits to {}".format(fname))
            if self.rank == 0:
                timer.report_clear("Write hits")

        if self.write_wcov_inv:
            fname = os.path.join(self.outdir, self.outprefix + "invnpp.fits")
            if self.zip_maps:
                fname += ".gz"
            self.white_noise_cov_matrix.write_healpix_fits(fname)
            if self.rank == 0:
                log.info("Wrote inverse white noise covariance to {}".format(fname))
            if self.rank == 0:
                timer.report_clear("Write N_pp'^1")

        # invert it
        covariance_invert(self.white_noise_cov_matrix, 1.0e-3)
        if self.rank == 0:
            timer.report_clear("Invert N_pp'^1")

        if self.write_wcov:
            fname = os.path.join(self.outdir, self.outprefix + "npp.fits")
            if self.zip_maps:
                fname += ".gz"
            self.white_noise_cov_matrix.write_healpix_fits(fname)
            if self.rank == 0:
                log.info("Wrote white noise covariance to {}".format(fname))
            if self.rank == 0:
                timer.report_clear("Write N_pp'")

        return
