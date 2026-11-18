"""Gera legendas ASS estilo shorts (bloco curto + palavra falada destacada) e créditos."""

from __future__ import annotations

from collections.abc import Sequence

from cortes.config import CaptionStyle
from cortes.domain.captions import CaptionEvent


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS usa ordem BGR e alpha invertido: 00 = opaco)."""
    value = hex_rgb.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Cor inválida: {hex_rgb!r} (use #RRGGBB)")
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def escape_text(text: str) -> str:
    # Chaves abrem blocos de override e "\" inicia comandos no ASS.
    return (
        text.replace("\\", "/")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def build_ass(
    events: Sequence[CaptionEvent],
    style: CaptionStyle,
    *,
    width: int,
    height: int,
    caption_top: int,
    credit: str | None,
    duration_s: float,
) -> str:
    primary = ass_color(style.color)
    outline = ass_color(style.outline_color)
    back = ass_color(style.outline_color, alpha=0x64)
    highlight = ass_color(style.highlight_color)[4:]  # override \c usa só BBGGRR
    scale = style.highlight_scale

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Alignment 8 = topo-centro: MarginV é a distância a partir do topo do quadro.
        f"Style: Caption,{style.font},{style.size},{primary},{primary},{outline},{back},"
        f"-1,0,0,0,100,100,0,0,1,{style.outline:g},{style.shadow:g},8,60,60,{caption_top},1",
        # Alignment 2 = base-centro.
        f"Style: Credit,{style.credit_font},{style.credit_size},{primary},{primary},{outline},"
        f"{back},-1,0,0,0,100,100,0,0,1,3,1,2,40,40,{style.credit_margin_bottom},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if credit:
        lines.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(duration_s)},Credit,,0,0,0,,{escape_text(credit)}"
        )

    for event in events:
        parts: list[str] = []
        for i, word in enumerate(event.words):
            text = escape_text(word)
            if i == event.highlight:
                parts.append(f"{{\\c&H{highlight}&\\fscx{scale}\\fscy{scale}}}{text}{{\\r}}")
            else:
                parts.append(text)
        lines.append(
            f"Dialogue: 0,{ass_time(event.start)},{ass_time(event.end)},Caption,,0,0,0,,"
            + " ".join(parts)
        )

    return "\n".join(lines) + "\n"
