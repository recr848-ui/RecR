"""上部バーのレベルメーター（スペクトラム/アナログ）と録音中表示の点滅"""

import tkinter as tk
import math


class MetersMixin:
    def _eq_segment_color(self, seg_index):
        # 上段ほど赤、中段は黄、下段は緑（ハードウェアVUメーター風の3色配色）
        ratio = (seg_index + 1) / self.EQ_NUM_SEGMENTS
        if ratio > 0.85:
            return "#ff4d4d"
        if ratio > 0.6:
            return "#f5c542"
        return "#3fdd6a"

    def _rebuild_eq_meter(self):
        """イコライザー表示モード（スペクトラム/ピークメーター）に応じてCanvasの中身を作り直す"""
        self.eq_canvas.delete("all")
        if self.eq_mode_var.get() == "peak":
            self._build_analog_peak_meter()
        else:
            self._build_led_spectrum()

    def _build_led_spectrum(self):
        """マルチバンドのLEDセグメント表示（スペクトラムアナライザー風）を構築"""
        self.eq_canvas.configure(width=self.EQ_WIDTH, height=self.EQ_HEIGHT)

        num_bands = self.manager.EQ_NUM_BANDS
        band_width = (self.EQ_WIDTH - (num_bands - 1) * self._eq_band_gap) / num_bands
        segment_height = (
            self.EQ_HEIGHT - (self.EQ_NUM_SEGMENTS - 1) * self._eq_segment_gap
        ) / self.EQ_NUM_SEGMENTS

        self._eq_segments = []
        self._eq_peak_rects = []
        self._eq_band_bounds = []
        for i in range(num_bands):
            x0 = i * (band_width + self._eq_band_gap)
            x1 = x0 + band_width
            self._eq_band_bounds.append((x0, x1))

            band_segments = []
            for s in range(self.EQ_NUM_SEGMENTS):
                seg_y1 = self.EQ_HEIGHT - s * (segment_height + self._eq_segment_gap)
                seg_y0 = seg_y1 - segment_height
                rect = self.eq_canvas.create_rectangle(
                    x0, seg_y0, x1, seg_y1, fill=self._eq_off_color, outline=""
                )
                band_segments.append((rect, self._eq_segment_color(s)))
            self._eq_segments.append(band_segments)

            peak_rect = self.eq_canvas.create_rectangle(
                x0, self.EQ_HEIGHT, x1, self.EQ_HEIGHT, fill="#ffffff", outline=""
            )
            self._eq_peak_rects.append(peak_rect)

        # VUメーター風に「素早く上昇・ゆっくり減衰」させて表示する現在値、
        # およびそれよりもゆっくり減衰して直近の最大値を示すピークホールド値
        self._eq_display_levels = [0.0] * num_bands
        self._eq_peak_levels = [0.0] * num_bands

    # アナログ針メーター（ピークメーター）のジオメトリ定数
    EQ_ANALOG_WIDTH = 200
    EQ_ANALOG_HEIGHT = 54
    EQ_ANALOG_RADIUS = 32
    EQ_ANALOG_ANGLE_MIN = -50  # 針の振れ角（垂直=0度、左がマイナス）
    EQ_ANALOG_ANGLE_MAX = 50

    def _analog_tk_angle(self, level):
        """0.0〜1.0のレベルをCanvas角度系（東=0度、反時計回りが正）の角度[度]に変換"""
        angle = self.EQ_ANALOG_ANGLE_MIN + level * (self.EQ_ANALOG_ANGLE_MAX - self.EQ_ANALOG_ANGLE_MIN)
        return 90 - angle

    def _build_analog_peak_meter(self):
        """アナログ針式のL/Rピークメーターを構築（ラックスマン等の角形VUメーター風）

        扇形（PIESLICE）ではなく、往年のオーディオアンプに見られる角形の窓に
        スケール弧を収めた見た目にする。スケール弧は針の可動域（angle_min〜max）
        と正確に一致させ、左右非対称な余白による「傾き」が出ないようにする。
        """
        self.eq_canvas.configure(width=self.EQ_ANALOG_WIDTH, height=self.EQ_ANALOG_HEIGHT)

        gauge_width = self.EQ_ANALOG_WIDTH / 2
        radius = self.EQ_ANALOG_RADIUS
        margin = 4

        scale_start = self._analog_tk_angle(1.0)
        scale_extent = self._analog_tk_angle(0.0) - scale_start

        self._eq_needles = []
        self._eq_pivots = []
        for ch, label in enumerate(("L", "R")):
            gx0 = gauge_width * ch
            rect_x0, rect_y0 = gx0 + margin, margin
            rect_x1, rect_y1 = gx0 + gauge_width - margin, self.EQ_ANALOG_HEIGHT - margin
            pivot_x = (rect_x0 + rect_x1) / 2
            pivot_y = rect_y1 - 6
            bbox = (pivot_x - radius, pivot_y - radius, pivot_x + radius, pivot_y + radius)

            # メーター面（乳白色の角窓。ラックスマン等のアンプに見られる意匠）
            self.eq_canvas.create_rectangle(
                rect_x0, rect_y0, rect_x1, rect_y1,
                fill="#f2ead9", outline="#1a1a1a", width=1
            )
            # スケール弧（針の可動域と完全に一致させ左右対称にする）
            self.eq_canvas.create_arc(
                *bbox, start=scale_start, extent=scale_extent,
                style=tk.ARC, outline="#4a4a4a", width=1
            )
            # レッドゾーン（レベル0.85〜1.0を縁取りで示す）
            red_start = self._analog_tk_angle(1.0)
            red_extent = self._analog_tk_angle(0.85) - red_start
            self.eq_canvas.create_arc(
                *bbox, start=red_start, extent=red_extent, style=tk.ARC,
                outline="#c1392b", width=3
            )
            # 目盛り
            for tick_level in (0.0, 0.25, 0.5, 0.75, 1.0):
                tick_angle = math.radians(self._analog_tk_angle(tick_level))
                x0 = pivot_x + (radius - 6) * math.cos(tick_angle)
                y0 = pivot_y - (radius - 6) * math.sin(tick_angle)
                x1 = pivot_x + radius * math.cos(tick_angle)
                y1 = pivot_y - radius * math.sin(tick_angle)
                self.eq_canvas.create_line(x0, y0, x1, y1, fill="#4a4a4a", width=1)

            # "VU" ロゴ風の文字とチャンネルラベル
            self.eq_canvas.create_text(
                pivot_x, rect_y0 + 9, text="VU",
                font=("Times New Roman", 8, "bold"), fill="#1a1a1a"
            )
            self.eq_canvas.create_text(
                rect_x0 + 8, rect_y1 - 7, text=label,
                font=("Yu Gothic UI", 7, "bold"), fill="#4a4a4a"
            )

            # 針（初期位置は0レベル）
            tick_angle = math.radians(self._analog_tk_angle(0.0))
            tip_x = pivot_x + radius * math.cos(tick_angle)
            tip_y = pivot_y - radius * math.sin(tick_angle)
            needle = self.eq_canvas.create_line(
                pivot_x, pivot_y, tip_x, tip_y, fill="#a02020", width=2
            )
            self.eq_canvas.create_oval(
                pivot_x - 3, pivot_y - 3, pivot_x + 3, pivot_y + 3,
                fill="#1a1a1a", outline=""
            )

            self._eq_needles.append(needle)
            self._eq_pivots.append((pivot_x, pivot_y))

        # アナログ針にはVUメーター同様「素早く上昇・ゆっくり減衰」の値のみを使い、
        # デジタル的なピークホールド表示は設けない（実機のアナログVUメーターに準拠）
        self._eq_display_levels = [0.0, 0.0]
        self._eq_peak_levels = [0.0, 0.0]

    def _on_eq_mode_changed(self):
        """イコライザー表示モード切り替え時: 設定を保存し、表示を作り直す"""
        self.manager.save_settings({'eq_mode': self.eq_mode_var.get()})
        self._rebuild_eq_meter()

    def _reset_eq(self):
        """イコライザーの表示を即座に0へリセットする"""
        num_channels = len(self._eq_display_levels)
        self._eq_display_levels = [0.0] * num_channels
        self._eq_peak_levels = [0.0] * num_channels
        self._update_eq()

    def _schedule_eq_update(self):
        """再生中、グラフィックイコライザーを一定間隔で再描画する

        録音時はデコードが実時間より速く進み更新が間欠的になるため使わない
        （録音中は代わりに「● REC」の点滅表示を使う。_schedule_recording_watch参照）。
        """
        self._update_eq()
        if self.manager.is_playing():
            self._eq_update_job = self.root.after(50, self._schedule_eq_update)
        else:
            # エラー等でバックグラウンド再生が止まっていたらUIも再生停止状態に戻す
            self._eq_update_job = None
            if str(self.play_stop_button['state']) == tk.NORMAL:
                self.stop_playback()

    def _start_rec_blink(self):
        """「● REC」インジケーターの点滅を開始する"""
        self._rec_blink_visible = True
        self._blink_rec_indicator()

    def _blink_rec_indicator(self):
        self.rec_indicator_var.set("● REC" if self._rec_blink_visible else "")
        self._rec_blink_visible = not self._rec_blink_visible
        self._rec_blink_job = self.root.after(500, self._blink_rec_indicator)

    def _stop_rec_blink(self):
        """「● REC」インジケーターの点滅を止めて非表示にする"""
        if self._rec_blink_job:
            self.root.after_cancel(self._rec_blink_job)
            self._rec_blink_job = None
        self.rec_indicator_var.set("")

    # 減衰速度（1回の描画更新あたりの減衰量）。値が大きいほど素早く0に落ちる
    EQ_DECAY_PER_TICK = 0.06
    # ピークホールド（直近の最大値を示す白いバー）の減衰速度。本体より緩やかに落とす
    EQ_PEAK_DECAY_PER_TICK = 0.012

    def _update_eq(self):
        """マネージャーが保持する最新のレベルでイコライザー表示を更新する（モードに応じて分岐）"""
        if self.eq_mode_var.get() == "peak":
            self._update_analog_peak_meter()
        else:
            self._update_led_spectrum()

    def _update_analog_peak_meter(self):
        """L/Rチャンネルレベルでアナログ針メーターの針を更新

        VUメーターと同様「素早く上昇・ゆっくり減衰」させることで、
        実際の針の慣性のような自然な動きに見せる。
        """
        levels = self.manager.get_lr_levels()
        radius = self.EQ_ANALOG_RADIUS
        for i, needle in enumerate(self._eq_needles):
            target = levels[i] if i < len(levels) else 0.0
            current = self._eq_display_levels[i]
            current = target if target >= current else max(target, current - self.EQ_DECAY_PER_TICK)
            self._eq_display_levels[i] = current

            pivot_x, pivot_y = self._eq_pivots[i]
            tick_angle = math.radians(self._analog_tk_angle(current))
            tip_x = pivot_x + radius * math.cos(tick_angle)
            tip_y = pivot_y - radius * math.sin(tick_angle)
            self.eq_canvas.coords(needle, pivot_x, pivot_y, tip_x, tip_y)

    def _update_led_spectrum(self):
        """マネージャーが保持する最新のバンドレベルでイコライザーのLEDセグメントを更新

        録音時はデコードが実時間より大幅に速く進む（再生時と違い音声出力デバイスに
        よる自然なペーシングが無い）ため、セグメント単位でレベルがまとめて更新され、
        その後の取得待ち時間中は値が変化しない「バースト＋停止」になりがちで、
        そのまま描画すると動きがギクシャクして見える。VUメーターと同様に
        「新しい値が高ければ即座に反映し、低ければゆっくり減衰させる」ことで、
        更新が間欠的でも見た目は滑らかにする。ピークホールド（白いバー）はさらに
        ゆっくり減衰させ、直近の最大値を一時的に示す。
        """
        levels = self.manager.get_levels()
        for i, band_segments in enumerate(self._eq_segments):
            target = levels[i] if i < len(levels) else 0.0
            current = self._eq_display_levels[i]
            current = target if target >= current else max(target, current - self.EQ_DECAY_PER_TICK)
            self._eq_display_levels[i] = current

            lit_count = round(current * self.EQ_NUM_SEGMENTS)
            for s, (rect, on_color) in enumerate(band_segments):
                self.eq_canvas.itemconfig(rect, fill=on_color if s < lit_count else self._eq_off_color)

            peak = self._eq_peak_levels[i]
            peak = current if current >= peak else max(0.0, peak - self.EQ_PEAK_DECAY_PER_TICK)
            self._eq_peak_levels[i] = peak

            x0, x1 = self._eq_band_bounds[i]
            peak_y1 = max(self.EQ_PEAK_HOLD_HEIGHT, self.EQ_HEIGHT - peak * self.EQ_HEIGHT)
            peak_y0 = peak_y1 - self.EQ_PEAK_HOLD_HEIGHT
            self.eq_canvas.coords(self._eq_peak_rects[i], x0, peak_y0, x1, peak_y1)
