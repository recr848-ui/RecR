"""RadiruManager（NHK聴き逃し）のダウンロード処理のテスト。

実際の配信やFFmpegには触れず、av.open を偽のコンテナに差し替えて
「通信断で欠けたファイルを完了扱いにしない」ことだけを確認する。
"""
from fractions import Fraction

import av
import pytest

import utils.radiru_manager as radiru_manager
from utils.radiru_manager import RadiruManager


class _FakePacket:
    def __init__(self, duration):
        self.dts = 0
        self.pts = 0
        self.duration = duration
        self.stream = None


class _FakeStream:
    time_base = Fraction(1, 1000)


class _FakeStreams:
    audio = [_FakeStream()]


class _FakeInput:
    """packets（_FakePacket または送出する例外）を順に返す入力コンテナの代用"""

    def __init__(self, total_seconds, packets):
        self.duration = int(total_seconds * 1_000_000)
        self.streams = _FakeStreams()
        self._packets = iter(packets)

    def demux(self, stream):
        for packet in self._packets:
            if isinstance(packet, Exception):
                raise packet
            yield packet

    def close(self):
        pass


class _FakeOutput:
    def add_stream_from_template(self, stream):
        return object()

    def mux(self, packet):
        pass

    def close(self):
        pass


def _download(monkeypatch, tmp_path, total_seconds, packets):
    def fake_open(target, mode="r", **kwargs):
        return _FakeOutput() if mode == "w" else _FakeInput(total_seconds, packets)

    monkeypatch.setattr(radiru_manager.av, "open", fake_open)
    manager = object.__new__(RadiruManager)
    manager.download_episode("http://x/index.m3u8", tmp_path / "out.m4a")


def test_download_episode_succeeds_when_full_length_is_saved(monkeypatch, tmp_path):
    _download(monkeypatch, tmp_path, 60, [_FakePacket(1000) for _ in range(60)])


def test_download_episode_fails_when_segments_were_skipped(monkeypatch, tmp_path):
    """通信断でセグメントが飛ばされ、配信側の尺より短く終わったら失敗にする"""
    with pytest.raises(RuntimeError, match="欠落"):
        _download(monkeypatch, tmp_path, 60, [_FakePacket(1000) for _ in range(40)])


def test_download_episode_fails_immediately_when_stalled(monkeypatch, tmp_path):
    """何も届かないまま読み出しが時間切れになったら、読み出しをやり直さず失敗にする"""
    packets = [_FakePacket(1000), av.error.ExitError(1, "Immediate exit requested")]
    packets += [_FakePacket(1000) for _ in range(59)]
    with pytest.raises(RuntimeError, match="途絶えた"):
        _download(monkeypatch, tmp_path, 60, packets)
