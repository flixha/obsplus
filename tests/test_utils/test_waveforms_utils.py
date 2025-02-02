"""
Tests for waveform utilities.
"""
import copy

import inspect
from pathlib import Path

import numpy as np
import obspy
import pandas as pd
import pytest
from obspy import UTCDateTime

import obsplus
from obsplus.constants import NSLC, WAVEFORM_REQUEST_DTYPES
from obsplus.exceptions import ValidationError
from obsplus.interfaces import WaveformClient
from obsplus.utils.time import to_timedelta64
from obsplus.utils.waveforms import (
    trim_event_stream,
    stream2contiguous,
    archive_to_sds,
    merge_traces,
    stream_bulk_split,
    get_waveform_client,
    get_waveform_bulk_df,
)
from obsplus.utils.testing import assert_streams_almost_equal


class TestGetWaveformClient:
    """tests for getting a waveform client from various objects."""

    def test_from_mseed_file(self, tmpdir):
        """A path to a file should return a stream from that file."""
        st = obspy.read()
        new_path = Path(tmpdir) / "stream.mseed"
        st.write(str(new_path), "mseed")
        client = get_waveform_client(new_path)
        assert isinstance(client, WaveformClient)

    def test_from_bank(self, default_wbank):
        """A waveform client should just return itself."""
        client = get_waveform_client(default_wbank)
        assert isinstance(client, WaveformClient)


class TestTrimEventStream:
    """ensure the trim_event waveforms function works"""

    # fixtures
    @pytest.fixture(scope="class")
    def stream_with_short_end(self):
        """
        Snip off some waveform from the end, return the new waveforms with
        the time the waveform was snipped.
        """
        st = obspy.read()
        t2 = st[0].stats.endtime
        new_t2 = t2 - 10
        st[0].trim(endtime=new_t2)
        return st, new_t2

    # tests
    def test_trimmed(self, stream_with_short_end):
        """
        test that the max time on the waveforms is t2
        """
        st, t2 = stream_with_short_end
        st_new = trim_event_stream(st, required_len=None)
        max_endtime = max([tr.stats.endtime.timestamp for tr in st_new])
        assert abs(max_endtime - t2.timestamp) < 0.1

    def test_disjointed_raises(self, disjointed_stream):
        """
        A disjointed waveforms should raise
        """
        with pytest.raises(ValueError) as e:
            trim_event_stream(disjointed_stream)
            assert "the following waveforms is disjointed" in str(e)

    def test_trim_tolerance(self, stream_with_short_end):
        """
        Ensure a value error is raised when the difference in start or
        end times exceeds the supplied trim tolerance.
        """
        with pytest.raises(ValueError) as e:
            trim_event_stream(stream_with_short_end[0], trim_tolerance=2.0)
        assert "trim tolerance" in str(e.value.args[0])

    def test_fragmented_stream(self, fragmented_stream):
        """test with streams that are fragmented"""
        with pytest.warns(UserWarning) as w:
            st = trim_event_stream(fragmented_stream)
        assert "seconds long" in str(w[0].message)
        stations = {tr.stats.station for tr in st}
        assert "BOB" not in stations

    def test_empty_stream(self):
        """Ensure an empty stream returns an empty stream."""
        st = obspy.Stream()
        out = trim_event_stream(st)
        assert isinstance(out, obspy.Stream)
        assert len(out) == 0

    def test_stream_with_duplicates_merged(self):
        """Duplicate streams should be merged."""
        st = obspy.read() + obspy.read()
        out = trim_event_stream(st, merge=None)
        assert len(out) == 3


class TestMergeStream:
    """Tests for obsplus' style for merging streams together."""

    @pytest.fixture()
    def gapped_high_sample_stream(self):
        """
        Create a stream which has two overlapping traces with high sampling
        rates.
        """
        # first trace
        stats1 = {
            "sampling_rate": 6000.0,
            "starttime": UTCDateTime(2017, 9, 23, 18, 50, 29, 715100),
            "endtime": UTCDateTime(2017, 9, 23, 18, 50, 31, 818933),
            "network": "XI",
            "station": "00037",
            "location": "00",
            "channel": "FL1",
        }
        data1 = np.random.rand(12624)
        tr1 = obspy.Trace(data=data1, header=stats1)
        # second trace
        stat2 = {
            "sampling_rate": 6000.0,
            "delta": 0.00016666666666666666,
            "starttime": UTCDateTime(2017, 9, 23, 18, 50, 31, 819100),
            "endtime": UTCDateTime(2017, 9, 23, 18, 50, 31, 973933),
            "npts": 930,
            "calib": 1.0,
            "network": "XI",
            "station": "00037",
            "location": "00",
            "channel": "FL1",
        }
        data2 = np.random.rand(930)
        tr2 = obspy.Trace(data=data2, header=stat2)
        return obspy.Stream(traces=[tr1, tr2])

    def convert_stream_dtype(self, st, dtype):
        """Convert datatypes on each trace in the stream."""
        st = st.copy()
        for tr in st:
            tr.data = tr.data.astype(dtype)
            assert tr.data.dtype == dtype
        return st

    def test_identical_streams(self):
        """ensure passing identical streams performs de-duplication."""
        st = obspy.read()
        st2 = obspy.read() + st + obspy.read()
        st_out = merge_traces(st2)
        assert st_out == st

    def test_adjacent_traces(self):
        """Traces that are one sample away in time should be merged together."""
        # create stream with traces adjacent in time and merge together
        st1 = obspy.read()
        st2 = obspy.read()
        for tr1, tr2 in zip(st1, st2):
            tr2.stats.starttime = tr1.stats.endtime + 1.0 / tr2.stats.sampling_rate
        st_in = st1 + st2
        out = merge_traces(st_in)
        assert len(out) == 3
        # should be the same as merge and split
        assert out == st_in.merge(1).split()

    def test_traces_with_overlap(self):
        """Trace with overlap should be merged together."""
        st1 = obspy.read()
        st2 = obspy.read()
        for tr1, tr2 in zip(st1, st2):
            tr2.stats.starttime = tr1.stats.starttime + 10
        st_in = st1 + st2
        out = merge_traces(st_in)
        assert out == st_in.merge(1).split()

    def test_traces_with_different_sampling_rates(self):
        """traces with different sampling_rates should be left alone."""
        st1 = obspy.read()
        st2 = obspy.read()
        for tr in st2:
            tr.stats.sampling_rate = tr.stats.sampling_rate * 2
        st_in = st1 + st2
        st_out = merge_traces(st_in)
        assert st_out == st_in

    def test_array_data_type(self):
        """The array datatype should not change."""
        # test floats
        st1 = obspy.read()
        st2 = obspy.read()
        st_out1 = merge_traces(st1 + st2)
        for tr1, tr2 in zip(st_out1, st1):
            assert tr1.data.dtype == tr2.data.dtype
        # tests ints
        st3 = self.convert_stream_dtype(st1, np.int32)
        st4 = self.convert_stream_dtype(st1, np.int32)
        st_out2 = merge_traces(st3 + st4)
        for tr in st_out2:
            assert tr.data.dtype == np.int32
        # def test one int one float
        st_out3 = merge_traces(st1 + st3)
        for tr in st_out3:
            assert tr.data.dtype == np.float64
        # ensure order of traces doesn't mater for dtypes
        st_out4 = merge_traces(st3 + st1)
        for tr in st_out4:
            assert tr.data.dtype == np.float64

    def test_merge_bingham_st(self, bingham_stream):
        """Ensure the bingham stream can be merged"""
        out = merge_traces(bingham_stream, inplace=False)
        cols = list(NSLC) + ["starttime", "endtime", "gap_time", "gap_samps"]
        gaps_df = pd.DataFrame(out.get_gaps(), columns=cols)
        # overlaps are indicated by negative gap times
        assert (gaps_df["gap_time"] > 0).all()

    def test_merge_high_sampling_rate(self, gapped_high_sample_stream):
        """Ensure high sampling rate overlapped data still work."""
        # if this runs the test passes due to unmerged assert in function
        merge_traces(gapped_high_sample_stream)


class TestStream2Contiguous:
    """test the stream2contiguous function works"""

    # helper functions
    @staticmethod
    def streams_are_equal(st1, st2):
        """
        Test that the streams are equal minus the processing attr of
        stats dict.
        """
        st1.sort()
        st2.sort()
        for tr1, tr2 in zip(st1.traces, st2.traces):
            if not np.array_equal(tr1.data, tr2.data):
                return False
            d1 = copy.deepcopy(tr1.stats)
            d1.pop("processing", None)
            d2 = copy.deepcopy(tr2.stats)
            d2.pop("processing", None)
            if not d1 == d2:
                return False
        return True

    # fixtures
    @pytest.fixture(scope="class")
    def one_trace_gap_overlaps_stream(self):
        """Return waveforms with a gap on one trace."""
        st = obspy.read()
        st1 = st.copy()
        st2 = st.copy()
        t1 = st[0].stats.starttime
        t2 = st[0].stats.endtime
        average = obspy.UTCDateTime((t1.timestamp + t2.timestamp) / 2.0)
        a1 = average - 1
        a2 = average + 1
        st1[0].trim(starttime=t1, endtime=a1)
        st2[0].trim(starttime=a2, endtime=t2)
        st = st1 + st2
        return st

    # tests
    def test_contiguous(self, basic_stream_with_gap):
        """Test basic functionality."""
        st, st1, st2 = basic_stream_with_gap
        out = stream2contiguous(st)
        assert inspect.isgenerator(out)
        slist = list(out)
        assert len(slist) == 2
        st_out_1 = slist[0]
        st_out_2 = slist[1]
        # lengths should be equal
        assert len(st_out_1) == len(st1)
        assert len(st_out_2) == len(st2)
        # streams should be equal
        assert self.streams_are_equal(st_out_1, st1)
        assert self.streams_are_equal(st_out_2, st2)

    def test_disjoint(self, disjointed_stream):
        """
        Ensure nothing is returned if waveforms have no times were all
        three channels have data.
        """
        out = stream2contiguous(disjointed_stream)
        assert inspect.isgenerator(out)
        slist = list(out)
        assert not len(slist)

    def test_one_trace_gap(self, one_trace_gap_overlaps_stream):
        """
        Ensure nothing is returned if waveforms has not times were all
        three channels have data.
        """
        st = one_trace_gap_overlaps_stream
        out = stream2contiguous(st)
        assert inspect.isgenerator(out)
        slist = list(out)
        assert len(slist) == 2
        for st_out in slist:
            assert not len(st_out.get_gaps())


class TestArchiveToSDS:
    """Tests for converting archives to SDS."""

    stream_process_count = 0

    def stream_processor(self, st):
        """A simple stream processor which increments the call count."""
        self.stream_process_count += 1
        return st

    @pytest.fixture(scope="class")
    def converted_archive(self, tmpdir_factory, ta_dataset):
        """Convert a dataset archive to a SDS archive."""
        out = tmpdir_factory.mktemp("new_sds")
        ds = ta_dataset
        wf_path = ds.waveform_path
        archive_to_sds(wf_path, out, stream_processor=self.stream_processor)
        # Because fixtures run in different context then tests this we
        # need to test that the stream processor ran here.
        assert self.stream_process_count
        return out

    @pytest.fixture(scope="class")
    def sds_wavebank(self, converted_archive):
        """Create a new WaveBank on the converted archive."""
        wb = obsplus.WaveBank(converted_archive)
        wb.update_index()
        return wb

    @pytest.fixture(scope="class")
    def old_wavebank(self, ta_dataset):
        """get the wavebank of the archive before converting to sds"""
        bank = ta_dataset.waveform_client
        assert isinstance(bank, obsplus.WaveBank)
        return bank

    def test_path_exists(self, converted_archive):
        """ensure the path to the new SDS exists"""
        path = Path(converted_archive)
        assert path.exists()

    def test_directory_not_empty(self, sds_wavebank, old_wavebank):
        """ensure the same date range is found in the new archive"""
        sds_index = sds_wavebank.read_index()
        old_index = old_wavebank.read_index()
        # start times and endtimes for old and new should be the same
        group_old = old_index.groupby(list(NSLC))
        group_sds = sds_index.groupby(list(NSLC))
        # ensure starttimes are the same
        old_start = group_old.starttime.min()
        sds_start = group_sds.starttime.min()
        assert np.allclose(old_start.view(np.int64), sds_start.view(np.int64))
        # ensure endtimes are the same
        old_end = group_old.endtime.max()
        sds_end = group_sds.endtime.max()
        assert np.allclose(old_end.view(np.int64), sds_end.view(np.int64))

    def test_each_file_one_trace(self, sds_wavebank):
        """ensure each file in the sds has exactly one channel"""
        index = sds_wavebank.read_index()
        for fi in index.path.unique():
            base = Path(sds_wavebank.bank_path) / fi[1:]
            st = obspy.read(str(base))
            assert len({tr.id for tr in st}) == 1


class TestStreamBulkSplit:
    """Tests for converting a trace to a list of Streams."""

    @pytest.fixture
    def bing_pick_bulk(self, bingham_catalog):
        """Create a dataframe from the bingham_test picks."""
        picks = obsplus.picks_to_df(bingham_catalog)
        df = picks[list(NSLC)]
        df["starttime"] = picks["time"] - to_timedelta64(1.011)
        df["endtime"] = picks["time"] + to_timedelta64(7.011)
        return df

    def get_bulk_from_stream(self, st, tr_inds, times):
        """
        Create a bulk argument from a stream for traces specified and
        relative times.
        """
        out = []
        for tr_ind, times in zip(tr_inds, times):
            tr = st[tr_ind]
            nslc = tr.id.split(".")
            t1 = tr.stats.starttime + times[0]
            t2 = tr.stats.endtime + times[1]
            out.append(tuple(nslc + [t1, t2]))
        return out

    def test_stream_bulk_split(self):
        """Ensure the basic stream to trace works."""
        # get bulk params
        st = obspy.read()
        t1, t2 = st[0].stats.starttime + 1, st[0].stats.endtime - 1
        nslc = st[0].id.split(".")
        bulk = [tuple(nslc + [t1, t2])]
        # create traces, check len
        streams = stream_bulk_split(st, bulk)
        assert len(streams) == 1
        # assert trace after trimming is equal to before
        t_expected = obspy.Stream([st[0].trim(starttime=t1, endtime=t2)])
        assert t_expected == streams[0]

    def test_empty_query_returns_empty(self):
        """An empty query should return an emtpy Stream"""
        st = obspy.read()
        out = stream_bulk_split(st, [])
        assert len(out) == 0
        out2 = stream_bulk_split(st, None)
        assert len(out2) == 0

    def test_empty_stream_returns_empty(self):
        """An empty stream should also return an empty stream"""
        st = obspy.read()
        t1, t2 = st[0].stats.starttime + 1, st[0].stats.endtime - 1
        nslc = st[0].id.split(".")
        bulk = [tuple(nslc + [t1, t2])]
        out = stream_bulk_split(obspy.Stream(), bulk)
        assert len(out) == 0

    def test_no_bulk_matches(self):
        """Test when multiple bulk parameters don't match any traces."""
        st = obspy.read()
        bulk = []
        for tr in st:
            utc = obspy.UTCDateTime("2017-09-18")
            t1, t2 = utc, utc
            bulk.append(tuple([*tr.id.split(".") + [t1, t2]]))
        out = stream_bulk_split(st, bulk)
        assert len(out) == len(bulk)
        for tr in out:
            assert isinstance(tr, obspy.Stream)

    def test_two_overlap(self):
        """
        Tests for when there is an overlap of available data and
        requested data but some data are not available.
        """
        # setup stream and bulk args
        st = obspy.read()
        duration = st[0].stats.endtime - st[0].stats.starttime
        bulk = self.get_bulk_from_stream(st, [0, 1], [[-5, -5], [-5, -5]])
        # request data, check durations
        out = stream_bulk_split(st, bulk)
        for st_out in out:
            assert len(st_out) == 1
            stats = st_out[0].stats
            out_duration = stats.endtime - stats.starttime
            assert np.isclose(duration - out_duration, 5)

    def test_two_inter(self):
        """Tests for getting data completely contained in available range."""
        # setup stream and bulk args
        st = obspy.read()
        duration = st[0].stats.endtime - st[0].stats.starttime
        bulk = self.get_bulk_from_stream(st, [0, 1], [[5, -5], [5, -5]])
        # request data, check durations
        out = stream_bulk_split(st, bulk)
        for st_out in out:
            assert len(st_out) == 1
            stats = st_out[0].stats
            out_duration = stats.endtime - stats.starttime
            assert np.isclose(duration - out_duration, 10)

    def test_two_intervals_same_stream(self):
        """Tests for returning two intervals in the same stream."""
        st = obspy.read()
        bulk = self.get_bulk_from_stream(st, [0, 0], [[0, -15], [15, 0]])
        out = stream_bulk_split(st, bulk)
        assert len(out) == 2
        for st_out in out:
            assert len(st_out) == 1
            stats = st_out[0].stats
            out_duration = stats.endtime - stats.starttime
            assert abs(out_duration - 15) <= stats.sampling_rate * 2

    def test_input_from_df(self, bing_pick_bulk, bingham_stream, bingham_dataset):
        """Ensure bulk can be formed from a dataframe."""
        st_client = bingham_dataset.waveform_client
        st_list = stream_bulk_split(bingham_stream, bing_pick_bulk)
        for st1, (_, ser) in zip(st_list, bing_pick_bulk.iterrows()):
            st2 = st_client.get_waveforms(*ser.to_list())
            assert_streams_almost_equal(st1, st2, allow_off_by_one=True)

    def test_fill_value(self):
        """test for filling values."""
        st_client = obspy.read()
        bulk = self.get_bulk_from_stream(st_client, [0], [[-10, -20]])
        out = stream_bulk_split(st_client, bulk, fill_value=0)[0]
        assert len(out) == 1
        # without fill value this would only be 10 sec long
        assert abs(abs(out[0].stats.endtime - out[0].stats.starttime) - 20) < 0.1


class TestGetWaveformBulk:
    """Tests for getting the waveform bulk dataframe."""

    times = (
        "2010-01-01",
        np.datetime64("2010-01-02"),
        obspy.UTCDateTime("2010-11-20"),
        obspy.UTCDateTime("2010-12-20").timestamp,
    )

    bulk1 = [
        ("UU", "TMU", "01", "HHZ", times[0], times[1]),
        ("UU", "NOQ", "01", "ENZ", times[2], times[3]),
    ]

    @pytest.fixture()
    def bulk_df(self):
        """Create a dataframe from bulk."""
        df = pd.DataFrame(self.bulk1, columns=list(WAVEFORM_REQUEST_DTYPES))
        out = get_waveform_bulk_df(df)
        return out

    def test_tuple(self):
        """Ensure standard tuples produce bulk df."""
        out = get_waveform_bulk_df(self.bulk1)
        assert isinstance(out, pd.DataFrame)
        assert len(out) == len(self.bulk1)

    def test_dict(self):
        """Ensure a list of dicts also works."""
        bulk_dict = []
        for bulk in self.bulk1:
            req_dict = {i: v for i, v in zip(WAVEFORM_REQUEST_DTYPES, bulk)}
            bulk_dict.append(req_dict)
        df = pd.DataFrame(bulk_dict)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == len(self.bulk1)

    def test_dataframe(self, bulk_df):
        """Ensure a datframe with no extra columns works."""
        out = get_waveform_bulk_df(bulk_df)
        assert isinstance(out, pd.DataFrame)
        assert len(out) == len(self.bulk1) == len(bulk_df)

    def test_dataframe_extra_column(self, bulk_df):
        """The dataframe should work even with out of order/extra columns."""
        df = bulk_df.copy()
        df["bob"] = "lightening"
        # reverse column order
        df = df[reversed(list(df.columns))]
        out = get_waveform_bulk_df(df)
        assert isinstance(out, pd.DataFrame)
        assert len(out) == len(self.bulk1) == len(bulk_df)
        # the new columns should have been dropped
        cols = list(WAVEFORM_REQUEST_DTYPES)
        assert list(out.columns) == cols

    def test_missing_column_raises(self, bulk_df):
        """A missing column should raise."""
        df = bulk_df.drop(columns=["network"])
        with pytest.raises(ValidationError, match="network"):
            get_waveform_bulk_df(df)
