from pathlib import Path
path = Path(r"e:\project\InfEngine\python\Infernux\engine\ui\node_graph_view.py")
text = path.read_text(encoding="utf-8")
old = '''        # Header label
        label = layout.node.data.get("label", layout.typedef.label)
        font_sz = self._zoom_font(14.0 if is_context else 13.0)
        sw_x1, sw_y1, sw_x2, sw_y2 = self._node_header_swatch_rect(layout)
        label_right = (
            sw_x1 - 6.0 * z
            if layout.typedef.show_header_color_swatch
            else (
                sx + w * 0.60
                if layout.typedef.category_label
                else sx + w - pad_x
            )
        )
        if is_context:
            # Title + "Particle" subtitle stacked like Unity VFX Context headers.
            title_bottom = sy + hdr_h * 0.62
            ctx.draw_text_aligned(
                sx + pad_x, sy + 2.0 * z, max(sx + pad_x + 1.0, sx + w - pad_x),
                title_bottom,
                label, *_TEXT_COLOR, 0.0, 0.55, font_sz, True,
            )
            ctx.draw_text_aligned(
                sx + pad_x, title_bottom - 1.0 * z, sx + w - pad_x, sy + hdr_h - 2.0 * z,
                "Particle", *_TEXT_DIM_COLOR, 0.0, 0.35, self._zoom_font(10.0), True,
            )
        else:
            ctx.draw_text_aligned(
                sx + pad_x, sy, max(sx + pad_x + 1.0, label_right), sy + hdr_h,
                label, *_TEXT_COLOR, 0.0, 0.5, font_sz, True,
            )

        if layout.typedef.show_header_color_swatch:
            ctx.draw_filled_rect(sw_x1, sw_y1, sw_x2, sw_y2, *accent, 2.0 * z)
            ctx.draw_rect(
                sw_x1,
                sw_y1,
                sw_x2,
                sw_y2,
                *_HEADER_COLOR_SWATCH_OUTLINE,
                max(1.0, 1.15 * z),
                2.0 * z,
            )
        elif layout.typedef.category_label:
            ctx.draw_text_aligned(
                sx + w * 0.56,
                sy,
                sx + w - pad_x,
                sy + hdr_h,
                layout.typedef.category_label,
                *_TEXT_DIM_COLOR,
                1.0,
                0.5,
                self._zoom_font(9.0),
                True,
            )

        # Subtitle (e.g. clip path)'''
# Find by unique markers in case comment dash differs
start = text.find('        # Header label\n')
if start < 0:
    raise SystemExit('start not found')
sub_marker = '        # Subtitle (e.g. clip path)'
end = text.find(sub_marker, start)
if end < 0:
    raise SystemExit('subtitle marker not found')
# include through the subtitle draw block end
end2 = text.find('        # Border', end)
if end2 < 0:
    raise SystemExit('border marker not found')
new = '''        # Header label — node name only (no category chips / Particle subtitle).
        label = layout.node.data.get("label", layout.typedef.label)
        font_sz = self._zoom_font(_NODE_FONT)
        sw_x1, sw_y1, sw_x2, sw_y2 = self._node_header_swatch_rect(layout)
        label_right = (
            sw_x1 - 6.0 * z
            if layout.typedef.show_header_color_swatch
            else sx + w - pad_x
        )
        ctx.draw_text_aligned(
            sx + pad_x,
            sy,
            max(sx + pad_x + 1.0, label_right),
            sy + hdr_h,
            label,
            *_TEXT_COLOR,
            0.0,
            0.5,
            font_sz,
            True,
        )

        if layout.typedef.show_header_color_swatch:
            ctx.draw_filled_rect(sw_x1, sw_y1, sw_x2, sw_y2, *accent, 2.0 * z)
            ctx.draw_rect(
                sw_x1,
                sw_y1,
                sw_x2,
                sw_y2,
                *_HEADER_COLOR_SWATCH_OUTLINE,
                max(1.0, 1.15 * z),
                2.0 * z,
            )

        # Subtitle (e.g. clip path) — same 18px face as the rest of the node.
        subtitle = "" if is_context else layout.node.data.get("subtitle", "")
        if subtitle:
            body_top = sy + hdr_h + 2 * z
            ctx.draw_text_aligned(
                sx + pad_x,
                body_top,
                sx + w - pad_x,
                body_top + _NODE_PIN_ROW_H * z,
                subtitle,
                *_TEXT_BODY_COLOR,
                0.0,
                0.0,
                font_sz,
                True,
            )

'''
text = text[:start] + new + text[end2:]
path.write_text(text, encoding='utf-8')
print('header block updated', start, end2)
