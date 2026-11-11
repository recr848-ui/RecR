"""録音ファイルへのタグ（タイトル・局名・番組情報など）書き込み"""

import os
from pathlib import Path


class MetadataMixin:
    def _write_metadata_tags(self, output_path, file_format, metadata):
        """録音済みファイルにタグ（番組名・出演者・局名・概要・番組画像など）を埋め込む

        file_format="aac" の場合、出力は生のADTS AACストリームでMP4コンテナでは
        ないが、ID3v2タグはファイル先頭に独立したチャンクとして追加されるだけなので、
        ADTSストリームの前に付与しても再生自体には影響しない（対応プレイヤーであれば
        タグも読み取れる。ただしWindows Explorerは.aac用のタグハンドラーを持たないため
        エクスプローラーのプロパティ列には表示されない）。
        file_format="m4a" はMP4コンテナのためExplorerでもタグが表示できる。

        タイトルには録音ファイル名（拡張子なし）を入れ、番組名はサブタイトルに入れる。
        """
        title = Path(output_path).stem
        subtitle = metadata.get('title')
        artist = metadata.get('artist')
        album = metadata.get('album')
        comment = metadata.get('comment')
        date = metadata.get('date')
        image_url = metadata.get('image_url')
        image_data = self.get_image(image_url) if image_url else None

        if file_format == "m4a":
            from mutagen.mp4 import MP4, MP4Cover

            tags = MP4(output_path)
            if title:
                tags['\xa9nam'] = [title]
            if artist:
                tags['\xa9ART'] = [artist]
            if album:
                tags['\xa9alb'] = [album]
            if comment:
                tags['\xa9cmt'] = [comment]
            if date:
                tags['\xa9day'] = [date]
            if image_data:
                ext = os.path.splitext(image_url.split("?")[0])[1].lower()
                fmt = MP4Cover.FORMAT_PNG if ext == ".png" else MP4Cover.FORMAT_JPEG
                tags['covr'] = [MP4Cover(image_data, imageformat=fmt)]
            tags.save()
            if subtitle:
                self._write_mp4_subtitle(output_path, subtitle)
            return

        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TIT3, TPE1, TALB, COMM, TDRC, APIC

        try:
            tags = ID3(output_path)
        except ID3NoHeaderError:
            tags = ID3()

        if image_data:
            ext = os.path.splitext(image_url.split("?")[0])[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            tags.setall('APIC', [APIC(
                encoding=3, mime=mime, type=3, desc='Cover', data=image_data
            )])

        if title:
            tags.setall('TIT2', [TIT2(encoding=3, text=[title])])
        if subtitle:
            tags.setall('TIT3', [TIT3(encoding=3, text=[subtitle])])
        if artist:
            tags.setall('TPE1', [TPE1(encoding=3, text=[artist])])
        if album:
            tags.setall('TALB', [TALB(encoding=3, text=[album])])
        if comment:
            tags.setall('COMM', [COMM(encoding=3, lang='jpn', desc='', text=[comment])])
        if date:
            tags.setall('TDRC', [TDRC(encoding=3, text=[date])])

        tags.save(output_path, v2_version=3)

    @staticmethod
    def _write_mp4_subtitle(output_path, subtitle):
        """MP4ファイルにWindowsエクスプローラーの「サブタイトル」を書き込む

        Windowsは「サブタイトル」をiTunes形式のタグ（ilst）ではなく、moov/udta内の
        Xtraボックスに "WM/SubTitle"（UTF-16LE文字列）として保存・読み取りする。
        mutagenはXtraを扱えないため、ここで直接Xtraボックスを作って差し込み、
        親ボックスのサイズとチャンクオフセット表（stco/co64）を補正する。
        mutagenがilstを保存した直後に呼ぶ前提（moov/udtaが必ず存在する）。
        """
        import struct
        from mutagen._util import resize_bytes
        from mutagen.mp4 import Atoms

        name = b'WM/SubTitle'
        value = subtitle.encode('utf-16-le') + b'\x00\x00'
        # 値: サイズ(4) + 型(2, 8=Unicode文字列) + データ
        value_entry = struct.pack('>IH', 6 + len(value), 8) + value
        # エントリ: サイズ(4) + 名前長(4) + 名前 + 値の個数(4) + 値
        entry_body = struct.pack('>I', len(name)) + name + struct.pack('>I', 1) + value_entry
        entry = struct.pack('>I', 4 + len(entry_body)) + entry_body
        xtra = struct.pack('>I4s', 8 + len(entry), b'Xtra') + entry

        with open(output_path, 'r+b') as f:
            atoms = Atoms(f)
            moov, udta = atoms.path(b'moov', b'udta')
            old = next((c for c in udta.children if c.name == b'Xtra'), None)
            if old is not None:
                offset, length = old.offset, old.length
            else:
                offset, length = udta.offset + udta.length, 0
            resize_bytes(f, length, len(xtra), offset)
            f.seek(offset)
            f.write(xtra)
            delta = len(xtra) - length
            if delta == 0:
                return

            for parent in (moov, udta):
                f.seek(parent.offset)
                size = struct.unpack('>I', f.read(4))[0]
                if size == 1:  # 64bitサイズ
                    f.seek(parent.offset + 8)
                    size = struct.unpack('>Q', f.read(8))[0]
                    f.seek(parent.offset + 8)
                    f.write(struct.pack('>Q', size + delta))
                else:
                    f.seek(parent.offset)
                    f.write(struct.pack('>I', size + delta))

            # Xtraより後ろにある音声データを指すチャンクオフセットをずらす
            for box_name, fmt_char in ((b'stco', 'I'), (b'co64', 'Q')):
                for atom in moov.findall(box_name, True):
                    atom_offset = atom.offset + (delta if atom.offset > offset else 0)
                    f.seek(atom_offset + 12)
                    count = struct.unpack('>I', f.read(4))[0]
                    fmt = '>%d%s' % (count, fmt_char)
                    offsets = struct.unpack(fmt, f.read(struct.calcsize(fmt)))
                    offsets = [o + delta if o > offset else o for o in offsets]
                    f.seek(atom_offset + 16)
                    f.write(struct.pack(fmt, *offsets))

    def write_metadata_tags(self, output_path, file_format, metadata):
        """_write_metadata_tagsの公開ラッパー（radiko以外の取得元でも同じタグ付けを使うため）"""
        return self._write_metadata_tags(output_path, file_format, metadata)
