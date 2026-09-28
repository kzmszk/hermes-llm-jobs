"""Allow-list sanitizer for LLM-drawn SVG line illustrations.

clean(text) finds the <svg>…</svg> in a model reply, keeps only the elements and attributes a
line drawing needs, and returns {'svg': str, 'memo': str?}. The first comment in the drawing
(the illustrator's design memo) is returned as plain text in 'memo' and every comment is dropped
from the SVG. Anything suspicious (DOCTYPE, processing instructions, script, event handlers,
external references, url() other than url(#id)) is removed, and a reply that is not a
well-formed SVG raises ValueError('invalid_result').
"""
import re
import xml.etree.ElementTree as ET

SVG_NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', SVG_NS)

ELEMENTS = {
    'svg', 'g', 'defs', 'filter', 'feTurbulence', 'feDisplacementMap', 'clipPath', 'use',
    'path', 'circle', 'ellipse', 'rect', 'line', 'polyline', 'polygon', 'text', 'tspan',
}
ATTRIBUTES = {
    'viewBox', 'id', 'x', 'y', 'width', 'height', 'dx', 'dy', 'transform', 'opacity',
    'type', 'baseFrequency', 'numOctaves', 'seed', 'result', 'in', 'in2', 'scale',
    'xChannelSelector', 'yChannelSelector', 'stitchTiles', 'filterUnits', 'primitiveUnits', 'clipPathUnits',
    'filter', 'clip-path', 'href', 'fill', 'fill-opacity', 'fill-rule', 'clip-rule',
    'stroke', 'stroke-width', 'stroke-opacity', 'stroke-linecap', 'stroke-linejoin',
    'stroke-dasharray', 'stroke-dashoffset', 'stroke-miterlimit', 'vector-effect', 'pathLength',
    'd', 'cx', 'cy', 'r', 'rx', 'ry', 'x1', 'y1', 'x2', 'y2', 'points', 'style',
    'font-size', 'font-family', 'font-weight', 'text-anchor', 'letter-spacing', 'dominant-baseline',
}
URL_REF = re.compile(r'url\(#[A-Za-z][\w.-]{0,40}\)')
PAINT = re.compile(r'(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|none|currentColor)')
ID = re.compile(r'[A-Za-z][\w.-]{0,40}')
SAFE_VALUE = re.compile(r'[\w\s.,#%()+\-/]*')          # no quotes, angle brackets, colons, semicolons or ampersands
MAX_ELEMENTS = 4000


def _local(name):
    return name.rsplit('}', 1)[-1]


def _attribute(name, value):
    """Return the value to keep for an allowed attribute, or None to drop it."""
    value = value.strip()
    if len(value) > 20000:
        return None
    if name == 'style':
        return 'mix-blend-mode:multiply' if re.fullmatch(r'mix-blend-mode\s*:\s*multiply\s*;?', value) else None
    if name == 'href':
        return value if re.fullmatch(r'#' + ID.pattern, value) else None
    if name in ('filter', 'clip-path'):
        return value if URL_REF.fullmatch(value) else None
    if name in ('fill', 'stroke'):
        return value if PAINT.fullmatch(value) else None
    if name == 'id':
        return value if ID.fullmatch(value) else None
    if 'url(' in value.lower() or not SAFE_VALUE.fullmatch(value):
        return None
    return value


def _copy(node, count):
    tag = _local(node.tag)
    if tag not in ELEMENTS:
        return None
    count[0] += 1
    if count[0] > MAX_ELEMENTS:
        raise ValueError('invalid_result')
    out = ET.Element(f'{{{SVG_NS}}}{tag}')
    for raw, value in node.attrib.items():
        name = _local(raw)                              # xlink:href → href
        if name in ATTRIBUTES:
            kept = _attribute(name, value)
            if kept is not None:
                out.set(name, kept)
    if tag in ('text', 'tspan') and node.text and node.text.strip():
        out.text = node.text.strip()[:200]
    for child in node:
        copied = _copy(child, count)
        if copied is not None:
            out.append(copied)
            if tag in ('text', 'tspan') and child.tail and child.tail.strip():
                copied.tail = child.tail.strip()[:200]
    return out


def clean(text, max_chars=24000):
    if not isinstance(text, str):
        raise ValueError('invalid_result')
    start, end = text.find('<svg'), text.rfind('</svg>')
    if start < 0 or end < start:
        raise ValueError('invalid_result')
    raw = text[start:end + 6]
    memo_match = re.search(r'<!--(.*?)-->', raw, re.S)
    memo = re.sub(r'\s+', ' ', memo_match.group(1)).strip()[:600] if memo_match else ''
    raw = re.sub(r'<!--.*?-->', '', raw, flags=re.S)
    if '<!' in raw or '<?' in raw:
        raise ValueError('invalid_result')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError('invalid_result')
    if _local(root.tag) != 'svg':
        raise ValueError('invalid_result')
    svg = _copy(root, [0])
    for size in ('width', 'height', 'x', 'y'):     # let the page size it from the viewBox
        svg.attrib.pop(size, None)
    if 'viewBox' not in svg.attrib:
        raise ValueError('invalid_result')
    out = ET.tostring(svg, encoding='unicode')
    if len(out) > max_chars:
        raise ValueError('invalid_result')
    result = {'svg': out}
    if memo:
        result['memo'] = memo
    return result
