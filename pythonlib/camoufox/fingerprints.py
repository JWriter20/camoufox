import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from random import Random, choice, randint, randrange, random, sample, shuffle
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from browserforge.fingerprints import (
    Fingerprint,
    FingerprintGenerator,
    ScreenFingerprint,
)

from camoufox.pkgman import load_yaml
from camoufox.webgl import sample_webgl

# Load the browserforge.yaml file
BROWSERFORGE_DATA = load_yaml('browserforge.yml')

FP_GENERATOR = FingerprintGenerator(browser='firefox', os=('linux', 'macos', 'windows'))

# Bundled real fingerprint presets
PRESETS_FILE = Path(__file__).parent / 'fingerprint-presets.json'
PRESETS_V150_FILE = Path(__file__).parent / 'fingerprint-presets-v150.json'
# Firefox major version at which the v150 preset bundle becomes preferred.
PRESETS_V150_MIN_FF = 149
_PRESETS_CACHE: Dict[Path, Dict] = {}

# CreepJS OS marker fonts used for OS detection. Twin of MARKER_FONTS in
# scripts/gen-fonts-json.py. Every name must be in fonts.json for its OS
# (scripts/verify-fonts.py checks): a marker the bundle cannot render would be a
# reverse leak. That is why PingFang HK/SC/TC are no longer macOS markers -- the
# bundle carries no PingFang file. On Linux the first three are also
# the families bundle/fontconfig/linux/fonts.conf resolves sans-serif / serif /
# monospace to, so an identity without them would have no face behind any CSS
# generic; Arimo / Cousine / Tinos stay because a real Ubuntu answers "present"
# for them via its metric aliases.
_MACOS_MARKER_FONTS = [
    'Helvetica Neue',
]
_LINUX_MARKER_FONTS = [
    'Noto Sans', 'Noto Serif', 'DejaVu Sans Mono', 'Arimo', 'Cousine', 'Tinos', 'Twemoji Mozilla',
]
_WINDOWS_MARKER_FONTS = [
    'Segoe UI', 'Tahoma', 'Cambria Math', 'Nirmala UI',
]


def _ensure_marker_fonts(fonts: List[str], markers: List[str]) -> None:
    """Add any missing marker fonts to the font list (in-place)."""
    existing = set(fonts)
    for m in markers:
        if m not in existing:
            fonts.append(m)


# OS font lists loaded from fonts.json
_OS_FONTS_CACHE: Optional[Dict[str, List[str]]] = None

def _load_os_fonts() -> Dict[str, List[str]]:
    """Load the full OS font lists from fonts.json."""
    global _OS_FONTS_CACHE
    if _OS_FONTS_CACHE is not None:
        return _OS_FONTS_CACHE
    fonts_path = os.path.join(os.path.dirname(__file__), 'fonts.json')
    with open(fonts_path, 'rb') as f:
        import orjson
        _OS_FONTS_CACHE = orjson.loads(f.read())
    return _OS_FONTS_CACHE


# Essential fonts per OS that must always be included in subsets.
#
# These are the OS BASE font sets: every family a real machine of that OS ships
# by default. A real machine has ALL of its OS defaults -- only the additions
# (Office, LibreOffice, Adobe CC, developer and web fonts) vary from box to box --
# so the base is never subsetted; the random 30-78% draw below applies to the
# additions only (fonts.json minus the base).
#
# Source: the per-OS bases in scripts/data/font-manifests.json (Windows 10 with
# the stock CJK families; macOS Sonoma; Ubuntu and its Mint variant),
# intersected with fonts.json so only names the bundle can
# render are listed (Sonoma's PingFang / Kefa / Hiragino families are in the real
# base but not bundled, so they are absent here). Regenerate together with
# fonts.json: `python3 scripts/gen-fonts-json.py --print-bases`.
#
# Windows: the seven GDI-substitution names (Courier, Helvetica, MS Sans Serif,
# MS Serif, Roman, Small Fonts, Times) and the six Light/Semilight names have no
# file of their own; bundle/fontconfig/windows/fonts.conf rewrites each to its
# bundled target unconditionally, so they MUST stay in this always-reported set
# (an identity that did not report Helvetica would still render it otherwise).
_ESSENTIAL_FONTS_MACOS = [
    # PingFang HK/SC/TC ship with every macOS (measured 2026-09-14: present on a
    # stock Mac mini in 5/5 runs, never drawn before).
    'PingFang HK', 'PingFang SC', 'PingFang TC',
    '.Al Bayan PUA', '.Al Nile PUA', '.Al Tarikh PUA', '.Apple Color Emoji UI',
    '.Apple SD Gothic NeoI', '.Aqua Kana', '.Aqua Kana Bold', '.Aqua かな', '.Aqua かな ボールド',
    '.Arial Hebrew Desk Interface', '.Baghdad PUA', '.Beirut PUA', '.Damascus PUA',
    '.DecoType Naskh PUA', '.Diwan Kufi PUA', '.Farah PUA', '.Geeza Pro Interface',
    '.Geeza Pro PUA', '.Hiragino Kaku Gothic Interface', '.Hiragino Sans GB Interface',
    '.Keyboard', '.KufiStandardGK PUA', '.LastResort', '.Lucida Grande UI', '.Muna PUA',
    '.Nadeem PUA', '.New York', '.Noto Nastaliq Urdu UI', '.SF Arabic', '.SF Arabic Rounded',
    '.SF Compact', '.SF Compact Rounded', '.SF NS', '.SF NS Mono', '.SF NS Rounded',
    '.Sana PUA', '.Savoye LET CC.', '.ThonburiUI', '.ThonburiUIWatch', 'Academy Engraved LET',
    'Al Bayan', 'Al Nile', 'Al Tarikh', 'American Typewriter', 'American Typewriter Semibold',
    'Andale Mono', 'Apple Braille', 'Apple Chancery', 'Apple Color Emoji',
    'Apple SD Gothic Neo', 'Apple SD Gothic Neo ExtraBold', 'Apple SD 산돌고딕 Neo',
    'Apple Symbols', 'AppleGothic', 'AppleMyungjo', 'Arial', 'Arial Black', 'Arial Hebrew',
    'Arial Hebrew Scholar', 'Arial Narrow', 'Arial Rounded MT Bold', 'Arial Unicode MS',
    'Athelas', 'Avenir', 'Avenir Black', 'Avenir Black Oblique', 'Avenir Book', 'Avenir Heavy',
    'Avenir Light', 'Avenir Medium', 'Avenir Next', 'Avenir Next Condensed',
    'Avenir Next Condensed Demi Bold', 'Avenir Next Condensed Heavy',
    'Avenir Next Condensed Medium', 'Avenir Next Condensed Ultra Light',
    'Avenir Next Demi Bold', 'Avenir Next Heavy', 'Avenir Next Medium',
    'Avenir Next Ultra Light', 'Ayuthaya', 'Baghdad', 'Bangla MN', 'Bangla Sangam MN',
    'Baskerville', 'Beirut', 'Big Caslon', 'Bodoni 72', 'Bodoni 72 Oldstyle',
    'Bodoni 72 Smallcaps', 'Bodoni Ornaments', 'Bradley Hand', 'Brush Script MT', 'Chalkboard',
    'Chalkboard SE', 'Chalkduster', 'Charter', 'Charter Black', 'Cochin', 'Comic Sans MS',
    'Copperplate', 'Corsiva Hebrew', 'Courier', 'Courier New', 'Czcionka systemowa',
    'DIN Alternate', 'DIN Condensed', 'Damascus', 'DecoType Naskh', 'Devanagari MT',
    'Devanagari Sangam MN', 'Didot', 'Diwan Kufi', 'Diwan Thuluth', 'Euphemia UCAS', 'Farah',
    'Farisi', 'Font Sistem', 'Font de sistem', 'Font di sistema', 'Font sustava',
    'Fonte do Sistema', 'Futura', 'Futura Bold', 'GB18030 Bitmap', 'Galvji', 'Geeza Pro',
    'Geneva', 'Georgia', 'Gill Sans', 'Grantha Sangam MN', 'Gujarati MT', 'Gujarati Sangam MN',
    'Gurmukhi MN', 'Gurmukhi MT', 'Gurmukhi Sangam MN', 'Heiti SC', 'Heiti TC', 'Heiti-간체',
    'Heiti-번체', 'Helvetica', 'Helvetica Neue', 'Herculanum', 'Hiragino Kaku Gothic Pro',
    'Hiragino Kaku Gothic Pro W3', 'Hiragino Kaku Gothic Pro W6', 'Hiragino Kaku Gothic ProN',
    'Hiragino Kaku Gothic ProN W3', 'Hiragino Kaku Gothic ProN W6', 'Hiragino Kaku Gothic Std',
    'Hiragino Kaku Gothic Std W8', 'Hiragino Kaku Gothic StdN', 'Hiragino Kaku Gothic StdN W8',
    'Hiragino Maru Gothic Pro', 'Hiragino Maru Gothic Pro W4', 'Hiragino Maru Gothic ProN',
    'Hiragino Maru Gothic ProN W4', 'Hiragino Mincho Pro', 'Hiragino Mincho Pro W3',
    'Hiragino Mincho Pro W6', 'Hiragino Mincho ProN', 'Hiragino Mincho ProN W3',
    'Hiragino Mincho ProN W6', 'Hiragino Sans', 'Hiragino Sans GB', 'Hiragino Sans GB W3',
    'Hiragino Sans GB W6', 'Hiragino Sans W0', 'Hiragino Sans W1', 'Hiragino Sans W2',
    'Hiragino Sans W3', 'Hiragino Sans W4', 'Hiragino Sans W5', 'Hiragino Sans W6',
    'Hiragino Sans W7', 'Hiragino Sans W8', 'Hiragino Sans W9', 'Hoefler Text',
    'Hoefler Text Ornaments', 'ITF Devanagari', 'ITF Devanagari Marathi', 'Impact', 'InaiMathi',
    'InaiMathi Bold', 'Iowan Old Style', 'Iowan Old Style Black', 'Järjestelmäfontti',
    'Kailasa', 'Kannada MN', 'Kannada Sangam MN', 'Khmer MN', 'Khmer Sangam MN',
    'Kohinoor Bangla', 'Kohinoor Devanagari', 'Kohinoor Devanagari Medium', 'Kohinoor Gujarati',
    'Kohinoor Telugu', 'Kokonor', 'Krungthep', 'KufiStandardGK', 'Lao MN', 'Lao Sangam MN',
    'Lucida Grande', 'Luminari', 'Malayalam MN', 'Malayalam Sangam MN', 'Marion', 'Marker Felt',
    'Menlo', 'Microsoft Sans Serif', 'Mishafi', 'Mishafi Gold', 'Monaco', 'Mshtakan',
    'Mukta Mahee', 'MuktaMahee Bold', 'MuktaMahee ExtraBold', 'MuktaMahee ExtraLight',
    'MuktaMahee Light', 'MuktaMahee Medium', 'MuktaMahee Regular', 'MuktaMahee SemiBold',
    'Muna', 'Myanmar MN', 'Myanmar Sangam MN', 'Nadeem', 'New Peninim MT', 'Noteworthy',
    'Noto Nastaliq Urdu', 'Noto Sans Adlam', 'Noto Sans Armenian', 'Noto Sans Armenian Blk',
    'Noto Sans Armenian ExtBd', 'Noto Sans Armenian ExtLt', 'Noto Sans Armenian Light',
    'Noto Sans Armenian Med', 'Noto Sans Armenian SemBd', 'Noto Sans Armenian Thin',
    'Noto Sans Avestan', 'Noto Sans Bamum', 'Noto Sans Bassa Vah', 'Noto Sans Batak',
    'Noto Sans Bhaiksuki', 'Noto Sans Brahmi', 'Noto Sans Buginese', 'Noto Sans Buhid',
    'Noto Sans CanAborig', 'Noto Sans Canadian Aboriginal',
    'Noto Sans Canadian Aboriginal Regular', 'Noto Sans Carian', 'Noto Sans CaucAlban',
    'Noto Sans Caucasian Albanian', 'Noto Sans Chakma', 'Noto Sans Cham', 'Noto Sans Coptic',
    'Noto Sans Cuneiform', 'Noto Sans Cypriot', 'Noto Sans Duployan', 'Noto Sans EgyptHiero',
    'Noto Sans Egyptian Hieroglyphs', 'Noto Sans Elbasan', 'Noto Sans Glagolitic',
    'Noto Sans Gothic', 'Noto Sans Gunjala Gondi', 'Noto Sans Hanifi Rohingya',
    'Noto Sans HanifiRohg', 'Noto Sans Hanunoo', 'Noto Sans Hatran', 'Noto Sans ImpAramaic',
    'Noto Sans Imperial Aramaic', 'Noto Sans InsPahlavi', 'Noto Sans InsParthi',
    'Noto Sans Inscriptional Pahlavi', 'Noto Sans Inscriptional Parthian', 'Noto Sans Javanese',
    'Noto Sans Kaithi', 'Noto Sans Kannada', 'Noto Sans Kannada Black',
    'Noto Sans Kannada ExtraBold', 'Noto Sans Kannada ExtraLight', 'Noto Sans Kannada Light',
    'Noto Sans Kannada Medium', 'Noto Sans Kannada SemiBold', 'Noto Sans Kannada Thin',
    'Noto Sans Kayah Li', 'Noto Sans Kharoshthi', 'Noto Sans Khojki', 'Noto Sans Khudawadi',
    'Noto Sans Lepcha', 'Noto Sans Limbu', 'Noto Sans Linear A', 'Noto Sans Linear B',
    'Noto Sans Lisu', 'Noto Sans Lycian', 'Noto Sans Lydian', 'Noto Sans Mahajani',
    'Noto Sans Mandaic', 'Noto Sans Manichaean', 'Noto Sans Marchen', 'Noto Sans Masaram Gondi',
    'Noto Sans Meetei Mayek', 'Noto Sans Mende Kikakui', 'Noto Sans Meroitic', 'Noto Sans Miao',
    'Noto Sans Modi', 'Noto Sans Mongolian', 'Noto Sans Mro', 'Noto Sans Multani',
    'Noto Sans Myanmar', 'Noto Sans Myanmar Blk', 'Noto Sans Myanmar ExtBd',
    'Noto Sans Myanmar ExtLt', 'Noto Sans Myanmar Light', 'Noto Sans Myanmar Med',
    'Noto Sans Myanmar SemBd', 'Noto Sans Myanmar Thin', 'Noto Sans NKo', 'Noto Sans Nabataean',
    'Noto Sans New Tai Lue', 'Noto Sans Newa', 'Noto Sans Ol Chiki', 'Noto Sans Old Hungarian',
    'Noto Sans Old Italic', 'Noto Sans Old North Arabian', 'Noto Sans Old Permic',
    'Noto Sans Old Persian', 'Noto Sans Old South Arabian', 'Noto Sans Old Turkic',
    'Noto Sans OldHung', 'Noto Sans OldNorArab', 'Noto Sans OldSouArab', 'Noto Sans Oriya',
    'Noto Sans Osage', 'Noto Sans Osmanya', 'Noto Sans Pahawh Hmong', 'Noto Sans Palmyrene',
    'Noto Sans Pau Cin Hau', 'Noto Sans PhagsPa', 'Noto Sans Phoenician',
    'Noto Sans PsaPahlavi', 'Noto Sans Psalter Pahlavi', 'Noto Sans Rejang',
    'Noto Sans Samaritan', 'Noto Sans Saurashtra', 'Noto Sans Sharada', 'Noto Sans Siddham',
    'Noto Sans Sora Sompeng', 'Noto Sans SoraSomp', 'Noto Sans Sundanese',
    'Noto Sans Syloti Nagri', 'Noto Sans Syriac', 'Noto Sans Tagalog', 'Noto Sans Tagbanwa',
    'Noto Sans Tai Le', 'Noto Sans Tai Tham', 'Noto Sans Tai Viet', 'Noto Sans Takri',
    'Noto Sans Thaana', 'Noto Sans Tifinagh', 'Noto Sans Tirhuta', 'Noto Sans Ugaritic',
    'Noto Sans Vai', 'Noto Sans Wancho', 'Noto Sans Warang Citi', 'Noto Sans Yi',
    'Noto Sans Zawgyi', 'Noto Sans Zawgyi Blk', 'Noto Sans Zawgyi ExtBd',
    'Noto Sans Zawgyi ExtLt', 'Noto Sans Zawgyi Light', 'Noto Sans Zawgyi Med',
    'Noto Sans Zawgyi SemBd', 'Noto Sans Zawgyi Thin', 'Noto Serif Ahom', 'Noto Serif Balinese',
    'Noto Serif Hmong Nyiakeng', 'Noto Serif Myanmar', 'Noto Serif Myanmar Blk',
    'Noto Serif Myanmar ExtBd', 'Noto Serif Myanmar ExtLt', 'Noto Serif Myanmar Light',
    'Noto Serif Myanmar Med', 'Noto Serif Myanmar SemBd', 'Noto Serif Myanmar Thin',
    'Noto Serif Yezidi', 'Optima', 'Oriya MN', 'Oriya Sangam MN', 'PT Mono', 'PT Sans',
    'PT Sans Caption', 'PT Sans Narrow', 'PT Serif', 'PT Serif Caption', 'Palatino', 'Papyrus',
    'Party LET', 'Phosphate', 'Phông chữ Hệ thống', 'Plantagenet Cherokee', 'Police système',
    'Raanana', 'Rendszerbetűtípus', 'Rockwell', 'STIX Two Math', 'STIX Two Math Regular',
    'STIX Two Text', 'STIX Two Text Regular', 'STIXGeneral', 'STIXIntegralsD',
    'STIXIntegralsSm', 'STIXIntegralsUp', 'STIXIntegralsUpD', 'STIXIntegralsUpSm',
    'STIXNonUnicode', 'STIXSizeFiveSym', 'STIXSizeFourSym', 'STIXSizeOneSym',
    'STIXSizeThreeSym', 'STIXSizeTwoSym', 'STIXVariants', 'STSong', 'Sana', 'Sathu',
    'Savoye LET', 'Seravek', 'Seravek ExtraLight', 'Seravek Light', 'Seravek Medium',
    'Shree Devanagari 714', 'SignPainter', 'SignPainter-HouseScript', 'Silom', 'Sinhala MN',
    'Sinhala Sangam MN', 'Sistem Fontu', 'Skia', 'Snell Roundhand', 'Songti SC', 'Songti TC',
    'Sukhumvit Set', 'Superclarendon', 'Symbol', 'Systeemlettertype', 'System Font',
    'Systemschrift', 'Systemskrift', 'Systemtypsnitt', 'Systémové písmo', 'Tahoma', 'Tamil MN',
    'Tamil Sangam MN', 'Telugu MN', 'Telugu Sangam MN', 'Thonburi', 'Times', 'Times New Roman',
    'Tipo de letra del sistema', 'Tipo de letra do sistema', 'Tipus de lletra del sistema',
    'Trattatello', 'Trebuchet MS', 'Verdana', 'Waseem', 'Webdings', 'Wingdings', 'Wingdings 2',
    'Wingdings 3', 'Zapf Dingbats', 'Zapfino', 'Γραμματοσειρά συστήματος', 'Системний шрифт',
    'Системный шрифт', 'גופן מערכת', 'البيان', 'التاريخ', 'النيل', 'بغداد', 'بيروت', 'جيزة',
    'خط النظام', 'دمشق', 'ديوان ثلث', 'ديوان كوفي', 'صنعاء', 'فارسي', 'فرح', 'كوفي', 'منى',
    'مِصحفي', 'مِصحفي ذهبي', 'نديم', 'نسخ', 'وسيم', 'कोहिनूर देवनागरी', 'แบบอักษรระบบ',
    'システムフォント', 'ヒラギノ丸ゴ Pro', 'ヒラギノ丸ゴ Pro W4', 'ヒラギノ丸ゴ ProN', 'ヒラギノ丸ゴ ProN W4', 'ヒラギノ明朝 Pro',
    'ヒラギノ明朝 Pro W3', 'ヒラギノ明朝 Pro W6', 'ヒラギノ明朝 ProN', 'ヒラギノ明朝 ProN W3', 'ヒラギノ明朝 ProN W6',
    'ヒラギノ角ゴ Pro', 'ヒラギノ角ゴ Pro W3', 'ヒラギノ角ゴ Pro W6', 'ヒラギノ角ゴ ProN', 'ヒラギノ角ゴ ProN W3',
    'ヒラギノ角ゴ ProN W6', 'ヒラギノ角ゴ Std', 'ヒラギノ角ゴ Std W8', 'ヒラギノ角ゴ StdN', 'ヒラギノ角ゴ StdN W8',
    'ヒラギノ角ゴ 簡体中文', 'ヒラギノ角ゴ 簡体中文 W3', 'ヒラギノ角ゴ 簡体中文 W6', 'ヒラギノ角ゴシック', 'ヒラギノ角ゴシック W0',
    'ヒラギノ角ゴシック W1', 'ヒラギノ角ゴシック W2', 'ヒラギノ角ゴシック W3', 'ヒラギノ角ゴシック W4', 'ヒラギノ角ゴシック W5',
    'ヒラギノ角ゴシック W6', 'ヒラギノ角ゴシック W7', 'ヒラギノ角ゴシック W8', 'ヒラギノ角ゴシック W9', '冬青黑体简体中文', '冬青黑体简体中文 W3',
    '冬青黑体简体中文 W6', '冬青黑體簡體中文', '冬青黑體簡體中文 W3', '冬青黑體簡體中文 W6', '宋体-简', '宋体-繁', '宋體-簡', '宋體-繁',
    '系統字體', '系统字体', '黑体-简', '黑体-繁', '黑體-簡', '黑體-繁', '黒体-簡', '黒体-繁', '시스템 서체',
]
_ESSENTIAL_FONTS_WINDOWS = [
    'Arial', 'Arial Black', 'Bahnschrift', 'Calibri', 'Calibri Light', 'Cambria',
    'Cambria Math', 'Candara', 'Candara Light', 'Comic Sans MS', 'Consolas', 'Constantia',
    'Corbel', 'Corbel Light', 'Courier', 'Courier New', 'Ebrima', 'Franklin Gothic Medium',
    'Gabriola', 'Gadugi', 'Georgia', 'Helvetica', 'Impact', 'Ink Free', 'Javanese Text',
    'Leelawadee UI', 'Leelawadee UI Semilight', 'Lucida Console', 'Lucida Sans Unicode',
    'MS Gothic', 'MS PGothic', 'MS Sans Serif', 'MS Serif', 'MS UI Gothic', 'MV Boli',
    'Malgun Gothic', 'Malgun Gothic Semilight', 'Marlett', 'Microsoft Himalaya',
    'Microsoft JhengHei', 'Microsoft JhengHei Light', 'Microsoft JhengHei UI',
    'Microsoft JhengHei UI Light', 'Microsoft New Tai Lue', 'Microsoft PhagsPa',
    'Microsoft Sans Serif', 'Microsoft Tai Le', 'Microsoft YaHei', 'Microsoft YaHei Light',
    'Microsoft YaHei UI', 'Microsoft YaHei UI Light', 'Microsoft Yi Baiti', 'MingLiU-ExtB',
    'MingLiU_HKSCS-ExtB', 'MingLiU_MSCS-ExtB', 'Mongolian Baiti', 'Myanmar Text', 'NSimSun',
    'Nirmala Text', 'Nirmala Text Semilight', 'Nirmala UI', 'Nirmala UI Semilight',
    'PMingLiU-ExtB', 'Palatino Linotype', 'Roman', 'Segoe MDL2 Assets', 'Segoe Print',
    'Segoe Script', 'Segoe UI', 'Segoe UI Black', 'Segoe UI Emoji', 'Segoe UI Historic',
    'Segoe UI Light', 'Segoe UI Semibold', 'Segoe UI Semilight', 'Segoe UI Symbol', 'SimSun',
    'SimSun-ExtB', 'Sitka Banner', 'Sitka Display', 'Sitka Heading', 'Sitka Small',
    'Sitka Subheading', 'Sitka Text', 'Small Fonts', 'Sylfaen', 'Symbol', 'Tahoma', 'Times',
    'Times New Roman', 'Trebuchet MS', 'Twemoji Mozilla', 'Verdana', 'Webdings', 'Wingdings',
    'Yu Gothic', 'Yu Gothic Light', 'Yu Gothic Medium', 'Yu Gothic UI', 'Yu Gothic UI Light',
    'Yu Gothic UI Semibold', 'Yu Gothic UI Semilight', '宋体', '微軟正黑體', '微軟正黑體 Light', '微软雅黑',
    '微软雅黑 Light', '新宋体', '新細明體-ExtB', '游ゴシック', '游ゴシック Light', '游ゴシック Medium', '細明體-ExtB',
    '細明體_HKSCS-ExtB', '細明體_MSCS-ExtB', '맑은 고딕', '맑은 고딕 Semilight', 'ＭＳ ゴシック', 'ＭＳ Ｐゴシック',
]
_ESSENTIAL_FONTS_LINUX = [
    # metric-compatible alias names a stock Linux fontconfig always resolves
    'Arial', 'Arial Narrow', 'Helvetica', 'Helvetica Narrow', 'Times', 'Times New Roman',
    'Courier', 'Courier New', 'Calibri', 'Cambria', 'Palatino', 'Palatino Linotype',
    'Bookman Old Style', 'Century Schoolbook', 'Avant Garde', 'Zapf Chancery', 'Symbol',
    'C059', 'D050000L', 'DejaVu Sans', 'DejaVu Sans Mono', 'DejaVu Serif',
    'Droid Sans Fallback', 'FreeMono', 'FreeSans', 'FreeSerif', 'Liberation Mono',
    'Liberation Sans', 'Liberation Serif', 'Nimbus Mono PS', 'Nimbus Roman', 'Nimbus Sans',
    'Nimbus Sans Narrow', 'Noto Color Emoji', 'Noto Kufi Arabic', 'Noto Looped Lao',
    'Noto Looped Thai', 'Noto Mono', 'Noto Music', 'Noto Naskh Arabic', 'Noto Nastaliq Urdu',
    'Noto Rashi Hebrew', 'Noto Sans', 'Noto Sans Adlam', 'Noto Sans Adlam Unjoined',
    'Noto Sans Anatolian Hieroglyphs', 'Noto Sans Arabic', 'Noto Sans Armenian',
    'Noto Sans Avestan', 'Noto Sans Balinese', 'Noto Sans Bamum', 'Noto Sans Bassa Vah',
    'Noto Sans Batak', 'Noto Sans Bengali', 'Noto Sans Bhaiksuki', 'Noto Sans Brahmi',
    'Noto Sans Buginese', 'Noto Sans Buhid', 'Noto Sans CJK HK', 'Noto Sans CJK JP',
    'Noto Sans CJK KR', 'Noto Sans CJK SC', 'Noto Sans CJK TC', 'Noto Sans Canadian Aboriginal',
    'Noto Sans Carian', 'Noto Sans Caucasian Albanian', 'Noto Sans Chakma', 'Noto Sans Cham',
    'Noto Sans Cherokee', 'Noto Sans Coptic', 'Noto Sans Cuneiform', 'Noto Sans Cypriot',
    'Noto Sans Deseret', 'Noto Sans Devanagari', 'Noto Sans Display', 'Noto Sans Duployan',
    'Noto Sans Egyptian Hieroglyphs', 'Noto Sans Elbasan', 'Noto Sans Elymaic',
    'Noto Sans Ethiopic', 'Noto Sans Georgian', 'Noto Sans Glagolitic', 'Noto Sans Gothic',
    'Noto Sans Grantha', 'Noto Sans Gujarati', 'Noto Sans Gunjala Gondi', 'Noto Sans Gurmukhi',
    'Noto Sans Hanifi Rohingya', 'Noto Sans Hanunoo', 'Noto Sans Hatran', 'Noto Sans Hebrew',
    'Noto Sans Imperial Aramaic', 'Noto Sans Indic Siyaq Numbers',
    'Noto Sans Inscriptional Pahlavi', 'Noto Sans Inscriptional Parthian', 'Noto Sans Javanese',
    'Noto Sans Kaithi', 'Noto Sans Kannada', 'Noto Sans Kayah Li', 'Noto Sans Kharoshthi',
    'Noto Sans Khmer', 'Noto Sans Khojki', 'Noto Sans Khudawadi', 'Noto Sans Lao',
    'Noto Sans Lepcha', 'Noto Sans Limbu', 'Noto Sans Linear A', 'Noto Sans Linear B',
    'Noto Sans Lisu', 'Noto Sans Lycian', 'Noto Sans Lydian', 'Noto Sans Mahajani',
    'Noto Sans Malayalam', 'Noto Sans Mandaic', 'Noto Sans Manichaean', 'Noto Sans Marchen',
    'Noto Sans Masaram Gondi', 'Noto Sans Math', 'Noto Sans Mayan Numerals',
    'Noto Sans Medefaidrin', 'Noto Sans Meetei Mayek', 'Noto Sans Mende Kikakui',
    'Noto Sans Meroitic', 'Noto Sans Miao', 'Noto Sans Modi', 'Noto Sans Mongolian',
    'Noto Sans Mono', 'Noto Sans Mono CJK HK', 'Noto Sans Mono CJK JP', 'Noto Sans Mono CJK KR',
    'Noto Sans Mono CJK SC', 'Noto Sans Mono CJK TC', 'Noto Sans Mro', 'Noto Sans Multani',
    'Noto Sans Myanmar', 'Noto Sans NKo', 'Noto Sans Nabataean', 'Noto Sans New Tai Lue',
    'Noto Sans Newa', 'Noto Sans Nushu', 'Noto Sans Ogham', 'Noto Sans Ol Chiki',
    'Noto Sans Old Hungarian', 'Noto Sans Old Italic', 'Noto Sans Old North Arabian',
    'Noto Sans Old Permic', 'Noto Sans Old Persian', 'Noto Sans Old Sogdian',
    'Noto Sans Old South Arabian', 'Noto Sans Old Turkic', 'Noto Sans Oriya', 'Noto Sans Osage',
    'Noto Sans Osmanya', 'Noto Sans Pahawh Hmong', 'Noto Sans Palmyrene',
    'Noto Sans Pau Cin Hau', 'Noto Sans PhagsPa', 'Noto Sans Phoenician',
    'Noto Sans Psalter Pahlavi', 'Noto Sans Rejang', 'Noto Sans Runic', 'Noto Sans Samaritan',
    'Noto Sans Saurashtra', 'Noto Sans Sharada', 'Noto Sans Shavian', 'Noto Sans Siddham',
    'Noto Sans SignWriting', 'Noto Sans Sinhala', 'Noto Sans Sogdian', 'Noto Sans Sora Sompeng',
    'Noto Sans Soyombo', 'Noto Sans Sundanese', 'Noto Sans Syloti Nagri', 'Noto Sans Symbols',
    'Noto Sans Symbols2', 'Noto Sans Syriac', 'Noto Sans Tagalog', 'Noto Sans Tagbanwa',
    'Noto Sans Tai Le', 'Noto Sans Tai Tham', 'Noto Sans Tai Viet', 'Noto Sans Takri',
    'Noto Sans Tamil', 'Noto Sans Tamil Supplement', 'Noto Sans Telugu', 'Noto Sans Thaana',
    'Noto Sans Thai', 'Noto Sans Tifinagh', 'Noto Sans Tifinagh APT',
    'Noto Sans Tifinagh Adrar', 'Noto Sans Tifinagh Agraw Imazighen',
    'Noto Sans Tifinagh Ahaggar', 'Noto Sans Tifinagh Air', 'Noto Sans Tifinagh Azawagh',
    'Noto Sans Tifinagh Ghat', 'Noto Sans Tifinagh Hawad', 'Noto Sans Tifinagh Rhissa Ixa',
    'Noto Sans Tifinagh SIL', 'Noto Sans Tifinagh Tawellemmet', 'Noto Sans Tirhuta',
    'Noto Sans Ugaritic', 'Noto Sans Vai', 'Noto Sans Wancho', 'Noto Sans Warang Citi',
    'Noto Sans Yi', 'Noto Sans Zanabazar Square', 'Noto Serif', 'Noto Serif Ahom',
    'Noto Serif Armenian', 'Noto Serif Balinese', 'Noto Serif Bengali', 'Noto Serif CJK HK',
    'Noto Serif CJK JP', 'Noto Serif CJK KR', 'Noto Serif CJK SC', 'Noto Serif CJK TC',
    'Noto Serif Devanagari', 'Noto Serif Display', 'Noto Serif Dogra', 'Noto Serif Ethiopic',
    'Noto Serif Georgian', 'Noto Serif Grantha', 'Noto Serif Gujarati', 'Noto Serif Gurmukhi',
    'Noto Serif Hebrew', 'Noto Serif Hmong Nyiakeng', 'Noto Serif Kannada', 'Noto Serif Khmer',
    'Noto Serif Khojki', 'Noto Serif Lao', 'Noto Serif Malayalam', 'Noto Serif Myanmar',
    'Noto Serif Sinhala', 'Noto Serif Tamil', 'Noto Serif Tamil Slanted', 'Noto Serif Tangut',
    'Noto Serif Telugu', 'Noto Serif Thai', 'Noto Serif Tibetan', 'Noto Serif Yezidi',
    'Noto Traditional Nushu', 'OpenSymbol', 'P052', 'Standard Symbols PS', 'URW Bookman',
    'URW Gothic', 'Ubuntu', 'Ubuntu Mono', 'Ubuntu Sans', 'Ubuntu Sans Mono', 'Z003',
]

# OS-version variants of the base, drawn ALL-OR-NOTHING on top of the essential
# core with the real-world share of that version (the manifest's base weights). A
# Windows 11 machine (65%) has every one of the Win11 additions and a Windows 10
# machine none of them; Ubuntu (65%) ships Liberation Sans Narrow, Mint (35%)
# does not. macOS has a single bundled base (Sonoma). Format: (probability, fonts).
_BASE_VARIANT_FONTS_MACOS = (0.0, [])
_BASE_VARIANT_FONTS_WINDOWS = (0.65, [
    'Cascadia Code', 'Cascadia Mono', 'Sans Serif Collection', 'Segoe Fluent Icons',
    'Segoe UI Variable Display', 'Segoe UI Variable Small', 'Segoe UI Variable Text',
])
_BASE_VARIANT_FONTS_LINUX = (0.65, ['Liberation Sans Narrow'])

# Fonts only a Windows 11 base has: a Windows identity whose font list contains
# them presents Windows 11, and the rest of the identity (overlay scrollbars,
# utils.launch_options) must agree.
WINDOWS_11_MARKER_FONTS = frozenset(_BASE_VARIANT_FONTS_WINDOWS[1])



def identity_seed(config: Dict[str, Any]) -> int:
    """A stable seed for the per-identity draws (fonts, voices).

    Two launches that present the same identity (same UA, platform, screen,
    cores, GPU) must present the same font and voice lists: a page that keeps
    cookies across launches and sees the font set or the voice list change
    under an otherwise identical device reads it as a spoofed browser
    (daijro/camoufox#442, #765, #378). Deriving the seed from the identity
    itself makes the draw a pure function of the fingerprint, so `from_options`
    replays and persistent contexts are stable without any new state.
    """
    import zlib
    parts = [
        str(config.get('navigator.userAgent', '')),
        str(config.get('navigator.platform', '')),
        str(config.get('screen.width', '')),
        str(config.get('screen.height', '')),
        str(config.get('navigator.hardwareConcurrency', '')),
        # not the GPU: it is sampled after the font draw in launch_options
    ]
    return zlib.crc32('|'.join(parts).encode('utf-8')) & 0xFFFFFFFF


def _rng(seed: Optional[int]) -> Random:
    """Seeded generator for a draw, or the module-level one when unseeded."""
    return Random(seed) if seed is not None else Random()


_FONT_GROUPS_CACHE: Optional[Dict[str, List[Dict[str, Any]]]] = None


def _load_font_groups() -> Dict[str, List[Dict[str, Any]]]:
    """Co-shipped font groups per OS (font-groups.json, derived from the
    bundle-kind additions of scripts/data/font-manifests.json): each entry is
    {"id": ..., "fonts": [...]} and is drawn all-or-nothing."""
    global _FONT_GROUPS_CACHE
    if _FONT_GROUPS_CACHE is None:
        path = os.path.join(os.path.dirname(__file__), 'font-groups.json')
        try:
            with open(path, 'rb') as f:
                _FONT_GROUPS_CACHE = json.loads(f.read())
        except (OSError, ValueError):
            _FONT_GROUPS_CACHE = {}
    return _FONT_GROUPS_CACHE


def _host_has_variant_fonts(target_os: str) -> bool:
    """Whether the host itself ships the OS-version font variant (native identities only)."""
    if target_os != 'windows':
        return False
    import os
    fonts_dir = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    # SegUIVar.ttf is Segoe UI Variable, present on every Windows 11 install and on no Windows 10.
    return os.path.exists(os.path.join(fonts_dir, 'SegUIVar.ttf'))


def _generate_random_font_subset(
    target_os: str, seed: Optional[int] = None, native: bool = False
) -> List[str]:
    """
    Generate a random subset of fonts for the given OS.
    Always includes the essential fonts (the OS base, i.e. every family a real
    machine of that OS ships), draws the OS-version variant of the base
    all-or-nothing, then picks a random percentage between 30-78% of the
    remaining fonts.json families (the additions), and finally ensures the
    marker fonts are present.

    `native`: the identity is the host's own OS (macOS / Windows), where the
    browser uses the real system fonts and not the bundle. Only the OS base
    is claimed then: an "addition" the host does not have would be listed but
    fall back when measured, which a page can see (measured 2026-09-14 on a
    stock Mac mini: Fira Code / Lato claimed, rendered as Menlo).
    """
    rng = _rng(seed)
    os_fonts_data = _load_os_fonts()
    os_key = {'macos': 'mac', 'windows': 'win', 'linux': 'lin'}.get(target_os, 'mac')
    full_list = os_fonts_data.get(os_key, os_fonts_data.get('mac', []))

    if target_os == 'windows':
        essential = set(_ESSENTIAL_FONTS_WINDOWS)
        markers = _WINDOWS_MARKER_FONTS
        variant_prob, variant_fonts = _BASE_VARIANT_FONTS_WINDOWS
    elif target_os == 'linux':
        essential = set(_ESSENTIAL_FONTS_LINUX)
        markers = _LINUX_MARKER_FONTS
        variant_prob, variant_fonts = _BASE_VARIANT_FONTS_LINUX
    else:
        essential = set(_ESSENTIAL_FONTS_MACOS)
        markers = _MACOS_MARKER_FONTS
        variant_prob, variant_fonts = _BASE_VARIANT_FONTS_MACOS
    variant = set(variant_fonts)

    # The base is always present in full. An essential family the bundle does
    # not carry (PingFang is Apple's, never redistributed) is still claimed:
    # on the host OS it is the real system font.
    result = [f for f in full_list if f in essential]
    if native:
        result.extend(sorted(f for f in essential if f not in set(full_list)))
        # The OS-version variant is real system fonts too: claim it exactly when
        # the host has it. A Windows 11 host presented as Windows 10 hides Segoe UI
        # Variable etc. and, with the matching classic scrollbars, differs from the
        # stock Firefox on the same machine (Windows 11 test host, 2026-09-16:
        # 0 px overlay).
        if variant and _host_has_variant_fonts(target_os):
            result.extend(f for f in variant_fonts if f not in result)
        return result

    # The OS-version variant of the base is all-or-nothing.
    if variant and rng.random() < variant_prob:
        result.extend(f for f in full_list if f in variant)

    # Everything else in fonts.json is an addition; draw a random subset of it.
    # Families that install as ONE download (Office, LibreOffice, Adobe CC,
    # Cascadia Code+Mono, Meslo LG S/M/L, ...) are drawn as a single unit so a
    # draw never produces a partial group -- a partial group is a synthetic
    # artifact no real machine shows (sundial "co-shipped families not split").
    # The 30-78% rule is applied over these units, not over bare family names.
    non_essential = [f for f in full_list if f not in essential and f not in variant]
    grouped: Dict[str, List[str]] = {}
    for group in _load_font_groups().get(os_key, []):
        members = [f for f in group['fonts'] if f in non_essential]
        for f in members:
            grouped[f] = members
    units: List[List[str]] = []
    seen = set()
    for f in non_essential:
        if f in seen:
            continue
        members = grouped.get(f, [f])
        seen.update(members)
        units.append(members)

    # Random percentage between 30-78%
    pct = 30 + int(rng.random() * 49)
    count = round((pct / 100) * len(units))

    # Randomly select non-essential units
    if count < len(units):
        chosen = rng.sample(units, count)
    else:
        chosen = units
    result.extend(f for unit in chosen for f in unit)

    # Ensure marker fonts are present
    _ensure_marker_fonts(result, markers)

    return result


# OS voice lists loaded from voices.json, parsed into "Name:lang:type" tuples.
_OS_VOICES_CACHE: Optional[Dict[str, List[Tuple[str, str, str]]]] = None


def _load_os_voices() -> Dict[str, List[Tuple[str, str, str]]]:
    """Load OS voice lists from voices.json as (name, lang, type) tuples.

    Each entry is "Name:lang:type" (type is "local" or "remote"). Voice names
    may contain parens/commas but not colons, so a last-two-colons split is
    safe.
    """
    global _OS_VOICES_CACHE
    if _OS_VOICES_CACHE is not None:
        return _OS_VOICES_CACHE
    voices_path = os.path.join(os.path.dirname(__file__), 'voices.json')
    with open(voices_path, 'rb') as f:
        import orjson
        raw = orjson.loads(f.read())
    _OS_VOICES_CACHE = {}
    for os_key, entries in raw.items():
        parsed: List[Tuple[str, str, str]] = []
        for entry in entries:
            last = entry.rfind(':')
            if last < 0:
                continue
            vtype = entry[last + 1:]
            before = entry[:last]
            langsep = before.rfind(':')
            if langsep < 0:
                continue
            lang = before[langsep + 1:]
            name = before[:langsep]
            if name and lang:
                parsed.append((name, lang, vtype))
        _OS_VOICES_CACHE[os_key] = parsed
    return _OS_VOICES_CACHE


# Essential speech voices per OS that must always be included in subsets
_ESSENTIAL_VOICES_MACOS = [
    'Samantha', 'Alex', 'Fred', 'Victoria', 'Karen', 'Daniel',
]
_ESSENTIAL_VOICES_WINDOWS = [
    'Microsoft David - English (United States)',
    'Microsoft Zira - English (United States)',
    'Microsoft Mark - English (United States)',
]

# Real Firefox speechSynthesis URI prefixes per backend.
#   macOS NSSpeechSynthesizer -> "urn:moz-tts:osx:<dotted-slug>"
#   Windows SAPI              -> "urn:moz-tts:sapi:<dotted-slug>"
#   Linux speech-dispatcher   -> "urn:moz-tts:speechd:<escaped-name>?<lang>"
_VOICE_URI_PREFIX = {
    'mac': 'urn:moz-tts:osx:',
    'win': 'urn:moz-tts:sapi:',
    'lin': 'urn:moz-tts:speechd:',
}


def _voice_uri_slug(name: str) -> str:
    """Stable dotted slug for mac/win URIs (shape-plausible, not catalog-exact)."""
    return re.sub(r'^\.|\.$', '', re.sub(r'[^a-z0-9]+', '.', name.lower()))


def _voice_uri(os_key: str, name: str, lang: str) -> str:
    """Build a voiceUri matching what real Firefox emits for the OS backend."""
    if os_key == 'lin':
        # Firefox's SpeechDispatcherService.cpp builds:
        #   "urn:moz-tts:speechd:" + NS_EscapeURL(name, OnlyNonASCII|Spaces) + "?" + lang
        # i.e. spaces -> %20 and non-ASCII bytes -> %XX, ASCII punctuation intact.
        escaped = []
        for ch in name:
            if ch == ' ':
                escaped.append('%20')
            elif ord(ch) <= 0x7F:
                escaped.append(ch)
            else:
                escaped.append(''.join(f'%{b:02X}' for b in ch.encode('utf-8')))
        return f"{_VOICE_URI_PREFIX['lin']}{''.join(escaped)}?{lang}"
    if os_key == 'win':
        # SapiService.cpp: "urn:moz-tts:sapi:" + name + "?" + lang, verbatim
        # (measured 2026-09-14 on a stock Windows 11: spaces and parentheses
        # unescaped, e.g. "...sapi:Microsoft David - English (United States)?en-US").
        return f"{_VOICE_URI_PREFIX['win']}{name}?{lang}"
    if os_key == 'mac':
        # OSXSpeechSynthesizerService: "urn:moz-tts:osx:" + AVSpeechSynthesisVoice
        # identifier. Catalogue captured from a stock macOS (voice-uris.json);
        # voices outside it follow Apple's identifier families.
        uri = _load_voice_uris().get('mac', {}).get(f'{name}|{lang}')
        if uri:
            return uri
        ascii_name = re.sub(r'[^A-Za-z0-9]', '', unicodedata.normalize('NFKD', name))
        if name in _MAC_NOVELTY_VOICES:
            # e.g. com.apple.speech.synthesis.voice.Albert / .Fred / .Victoria
            # (capitalised as the voice name; multi-word names are joined)
            return f"{_VOICE_URI_PREFIX['mac']}com.apple.speech.synthesis.voice.{ascii_name}"
        if name in _MAC_ELOQUENCE_VOICES:
            return f"{_VOICE_URI_PREFIX['mac']}com.apple.eloquence.{lang}.{ascii_name}"
        return f"{_VOICE_URI_PREFIX['mac']}com.apple.voice.compact.{lang}.{ascii_name}"
    return f"{_VOICE_URI_PREFIX.get(os_key, '')}{_voice_uri_slug(name)}"


_MAC_NOVELTY_VOICES = frozenset(
    {'Albert', 'Bad News', 'Bahh', 'Bells', 'Boing', 'Bubbles', 'Cellos', 'Wobble', 'Good News', 'Jester',
     'Organ', 'Superstar', 'Trinoids', 'Whisper', 'Zarvox', 'Fred', 'Junior', 'Kathy', 'Ralph',
     'Bruce', 'Vicki', 'Victoria', 'Agnes', 'Princess', 'Hysterical', 'Pipe Organ', 'Deranged'}
)
_MAC_ELOQUENCE_VOICES = frozenset({'Eddy', 'Flo', 'Grandma', 'Grandpa', 'Reed', 'Rocko', 'Sandy', 'Shelley'})
_VOICE_URIS_CACHE: Optional[Dict[str, Dict[str, str]]] = None


def _load_voice_uris() -> Dict[str, Dict[str, str]]:
    """Real voiceURI per "Name|lang" as a stock browser reports it (voice-uris.json)."""
    global _VOICE_URIS_CACHE
    if _VOICE_URIS_CACHE is None:
        path = os.path.join(os.path.dirname(__file__), 'voice-uris.json')
        try:
            with open(path, 'rb') as f:
                _VOICE_URIS_CACHE = json.loads(f.read())
        except OSError:
            _VOICE_URIS_CACHE = {}
    return _VOICE_URIS_CACHE


def _load_voice_manifests() -> Dict[str, Any]:
    """The per-OS installed-voice model (voice-manifests.json): a base the OS
    always ships, Windows language packs keyed by display locale, and additions
    drawn as atomic bundles / a-la-carte voices / whole language packs."""
    global _VOICE_MANIFESTS_CACHE
    if _VOICE_MANIFESTS_CACHE is None:
        path = os.path.join(os.path.dirname(__file__), 'voice-manifests.json')
        with open(path, 'rb') as f:
            _VOICE_MANIFESTS_CACHE = json.loads(f.read())
    return _VOICE_MANIFESTS_CACHE


_VOICE_MANIFESTS_CACHE: Optional[Dict[str, Any]] = None


def _split_voice_entry(entry: str) -> Tuple[str, str, str]:
    name, lang, vtype = entry.rsplit(':', 2)
    return name, lang, vtype


def _weighted_pick(rng: Random, items: List[Dict[str, Any]], wkey: str = 'w') -> Dict[str, Any]:
    total = sum(float(i.get(wkey, 0)) for i in items)
    r = rng.random() * total
    for i in items:
        r -= float(i.get(wkey, 0))
        if r <= 0:
            return i
    return items[-1]


def _weighted_sample(rng: Random, items: List[Any], k: int, weight) -> List[Any]:
    pool = list(items)
    out: List[Any] = []
    while pool and len(out) < k:
        total = sum(weight(x) for x in pool)
        r = rng.random() * total
        for x in pool:
            r -= weight(x)
            if r <= 0:
                out.append(x)
                pool.remove(x)
                break
        else:
            out.append(pool.pop())
    return out


def _resolve_display_pack(packs: Dict[str, Any], fallback: str, locale: Optional[str]) -> str:
    if locale:
        if locale in packs:
            return locale
        lang = locale.split('-')[0].lower()
        for key in packs:
            if key.split('-')[0].lower() == lang:
                return key
    return fallback if fallback in packs else next(iter(packs))


def _generate_random_voice_subset(
    target_os: str, locale: Optional[str] = None, seed: Optional[int] = None) -> List[Dict[str, Any]]:
    """Generate the speech voice list for the given OS as MaskConfig objects.

    Returns a list of {lang, name, voiceUri, isDefault, isLocalService} dicts,
    the shape MaskConfig::MVoices() requires (it silently drops any entry
    missing a field, so raw name strings would register nothing).

    Without this override, Firefox registers the HOST machine's
    speech-dispatcher / SAPI / NSSpeech voices, leaking the OS the wrapper
    actually runs on. The list follows a measured model of what a stock
    machine exposes (voice-manifests.json):
      Windows: the display language's OneCore pack (en-US: David/Mark/Zira),
               its legacy "Desktop" tokens, and occasionally extra packs;
      macOS:   the compact + Eloquence base (~184 voices) plus rare downloads;
      Linux:   speech-dispatcher's fixed espeak-ng list (131 voices).
    Seeded by the identity so the same identity always reports the same list.
    """
    rng = _rng(seed)
    os_key = {'macos': 'mac', 'windows': 'win', 'linux': 'lin'}.get(target_os, 'mac')
    manifest = _load_voice_manifests().get(os_key) or _load_voice_manifests()['mac']

    out: List[str] = []
    seen = set()

    def add(entries):
        for e in entries or []:
            if e not in seen:
                seen.add(e)
                out.append(e)

    legacy: List[str] = []
    packs = manifest.get('langPacks') or {}

    def take_pack(pack):
        add(pack.get('oneCore'))
        if pack.get('desktop') and rng.random() < float(pack.get('desktopProb') or 0):
            for e in pack['desktop']:
                if e not in legacy:
                    legacy.append(e)

    add(manifest.get('base'))
    chosen = set()
    if packs:
        key = _resolve_display_pack(packs, manifest.get('fallbackLocale') or 'en-US', locale)
        chosen.add(key)
        take_pack(packs[key])

    for addition in manifest.get('additions', []):
        if addition.get('deferred'):
            continue
        req = addition.get('requiresLocale')
        if req and not (locale or '').lower().startswith(req.lower()):
            continue
        if rng.random() >= float(addition.get('prob') or 0):
            continue
        kind = addition.get('kind')
        if kind == 'bundle':
            add(addition.get('voices'))
        elif kind == 'alacarte':
            sizes = addition.get('sizes') or [{'n': 1, 'w': 1}]
            k = int(_weighted_pick(rng, sizes)['n'])
            for e in _weighted_sample(rng, addition.get('voices') or [], k, lambda x: 1.0):
                if e not in seen:
                    seen.add(e)
                    # a downloaded voice sits in its alphabetical place
                    idx = next((i for i, v in enumerate(out) if v.lower() > e.lower()), len(out))
                    out.insert(idx, e)
        elif kind == 'groups' and packs:
            eligible = [g for g in addition.get('groups') or [] if g in packs and g not in chosen]
            if not eligible:
                continue
            k = int(_weighted_pick(rng, addition['sizes'])['n']) if addition.get('sizes') else len(eligible)
            for g in _weighted_sample(rng, eligible, k, lambda g: float(packs[g].get('weight') or 0.01)):
                chosen.add(g)
                take_pack(packs[g])

    selected = [_split_voice_entry(e) for e in out + legacy]
    if not selected:
        return []

    voices: List[Dict[str, Any]] = [
        {
            'name': name,
            'lang': lang,
            'voiceUri': _voice_uri(os_key, name, lang),
            'isDefault': False,
            'isLocalService': vtype == 'local',
        }
        for (name, lang, vtype) in selected
    ]

    # No voice carries default=true: stock Firefox 152 marks none on Windows
    # (SAPI), macOS or Linux (measured 2026-09-14 on all three), so a spoofed
    # default would be the odd one out.
    return voices
    if os_key == 'mac':
        pref = next((i for i, v in enumerate(voices) if v['name'] in ('Samantha', 'Alex') and (not locale or v['lang'].lower() == locale.lower())), -1)
        if pref >= 0:
            voices[pref]['isDefault'] = True
            return voices
    # Mark a default voice matching the spoofed locale prefix so it lines up
    # with Intl.DateTimeFormat().resolvedOptions().locale (CreepJS flags a
    # voiceLangMismatch otherwise).
    if voices:
        prefix = locale.split('-')[0].lower() if locale else 'en'
        idx = next(
            (i for i, v in enumerate(voices) if locale and v['lang'].lower() == locale.lower()),
            -1,
        )
        if idx < 0:
            idx = next(
                (i for i, v in enumerate(voices) if v['lang'].split('-')[0].lower() == prefix),
                -1,
            )
        if idx < 0:
            idx = 0
        voices[idx]['isDefault'] = True

    return voices


def _normalize_preset_voices(
    voices: Any, target_os: str
) -> List[Dict[str, Any]]:
    """Coerce a preset's `speechVoices` into MaskConfig voice objects.

    Presets historically store voices as "Name:lang:type" strings, which the
    C++ MaskConfig::MVoices() silently drops (it needs full objects). Convert
    them; pass through entries that are already objects.
    """
    os_key = {'macos': 'mac', 'windows': 'win', 'linux': 'lin'}.get(target_os, 'mac')
    result: List[Dict[str, Any]] = []
    for entry in voices:
        if isinstance(entry, dict):
            result.append(entry)
            continue
        last = entry.rfind(':')
        if last < 0:
            continue
        vtype = entry[last + 1:]
        before = entry[:last]
        langsep = before.rfind(':')
        if langsep < 0:
            continue
        lang = before[langsep + 1:]
        name = before[:langsep]
        if not name or not lang:
            continue
        result.append(
            {
                'name': name,
                'lang': lang,
                'voiceUri': _voice_uri(os_key, name, lang),
                'isDefault': False,
                'isLocalService': vtype == 'local',
            }
        )
    if result and not any(v['isDefault'] for v in result):
        result[0]['isDefault'] = True
    return result


def host_cpu_count() -> Optional[int]:
    """Logical CPUs this process may actually run on (cgroup/affinity aware)."""
    try:
        return len(os.sched_getaffinity(0)) or None  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return os.cpu_count()


# Core counts real desktop machines ship with, taken from the RECORDED
# fingerprint corpus rather than invented: fingerprint-presets.json and
# -v150.json between them contain 2, 4, 6, 8, 10, 12, 14, 16, 20 and 24.
#
# 24 was missing from this table and is restored (2026-09-15): it is a real
# recorded value on Windows (2/75) and Linux (2/18), and excluding it snapped
# genuine 24-core machines down to 20 for no reason.
#
# 2 is recorded too -- and is common, 6/30 macOS presets (20%) -- but is
# deliberately EXCLUDED (user, 2026-09-15): 2 is what Firefox reports under
# resistFingerprinting, and the goal is to look like a DEFAULT Firefox, which
# RFP is not. So a draw of 2 snaps up to the table floor of 4.
#
# A host outside this table would hand its own oddity to the fingerprint: a
# 64-thread build box reports 24, anything under 4 threads reports 4. Odd
# counts (5, 7, 9, 11, 13, 15) never appear in the corpus -- they are
# browserforge Bayesian synthesis -- so they keep getting snapped down.
PLAUSIBLE_CORE_COUNTS = (4, 6, 8, 10, 12, 14, 16, 20, 24)


def fix_hardware_concurrency(config: Dict[str, Any]) -> None:
    """navigator.hardwareConcurrency = the host's parallelism, snapped DOWN
    into PLAUSIBLE_CORE_COUNTS.

    A drawn value that differs from the machine the browser runs on is
    measurable from a page: timing N parallel workers reveals how many cores
    are really usable, and both "more usable than reported" and "fewer usable
    than reported" are flagged by WebCPU-style checks (sundial "CPU: reported
    vs measured cores"; daijro/camoufox#442 for the drift across launches).
    So the drawn value is discarded, not clamped: min(drawn, host) still lets
    a draw of 2 be measured as 16.

    Stock Firefox 152 reports the true count in a normal window (capped by
    dom.maxHardwareConcurrency = 128; measured 2026-09-14: 16-thread Linux and
    Windows hosts -> 16, a 10-core Mac mini -> 10). Its 8/4 tiering
    (RFPTarget NavigatorHWConcurrencyTiered: >= 8 -> 8, else 4) applies only
    with fingerprinting protection on, i.e. private windows and ETP strict,
    and resistFingerprinting hardcodes 4 (8 on macOS) -- neither is a normal
    window's behaviour, so nothing is rounded here beyond the table snap.
    The two tails (host > 20 or < 4) are the residual where reported and
    measurable can disagree; closing them needs CPU affinity pinning, not a
    launcher value. A caller that sets navigator.hardwareConcurrency
    themselves is left alone (see the _user_set_navigator guard).
    """
    n = host_cpu_count()
    if not n:
        return
    # The fingerprint's own value survives when the browser can be pinned to
    # that many cores (cpu_affinity: Linux, Windows): reported and measurable
    # then agree by construction, and the identity keeps its diversity. A draw
    # the host cannot honour (more cores than it has), or a host that cannot
    # pin (macOS), falls back to the snapped host count.
    from .cpu_affinity import supported as _can_pin

    cap = int(n)
    host_allowed = [c for c in PLAUSIBLE_CORE_COUNTS if c <= cap]
    host_value = host_allowed[-1] if host_allowed else PLAUSIBLE_CORE_COUNTS[0]

    drawn = config.get('navigator.hardwareConcurrency')
    if _can_pin() and isinstance(drawn, int) and drawn >= 1:
        # The fingerprint's value is kept for diversity, but it still has to be
        # a count a real desktop ships with. Accepting any 1..host let
        # browserforge's low/odd draws through: over 400 linux draws, 8.0% were
        # < 4 cores and 4.2% were exactly 2 -- and hardwareConcurrency == 2 is
        # the value Firefox reports under resistFingerprinting, so CreepJS-style
        # heuristics label the browser "Firefox resistFingerprinting" (this is
        # what intermittently failed sundial's "Privacy mode verdict"). Odd
        # counts (5, 7, 9, 11, 13, 15) survived the same way. Snap the draw DOWN
        # into the table instead, capped by the host so pinning can honour it.
        target = min(drawn, cap)
        allowed = [c for c in PLAUSIBLE_CORE_COUNTS if c <= target]
        config['navigator.hardwareConcurrency'] = (
            allowed[-1] if allowed else min(PLAUSIBLE_CORE_COUNTS[0], cap)
        )
        return
    config['navigator.hardwareConcurrency'] = host_value


def fix_navigator_arch(config: Dict[str, Any], target_os: str) -> None:
    """Force navigator.platform AND navigator.oscpu to match the UA's arch.

    ~8% of Linux Firefox fingerprints in the BrowserForge pool report
    "Linux armv81" for platform/oscpu while the UA says "Linux x86_64". That
    arch mismatch is itself a CreepJS lie signal (CreepJS cross-checks oscpu,
    platform, and the UA arch). Mac/Windows pools are consistent and need no
    correction.
    """
    if target_os != 'lin':
        return
    ua = config.get('navigator.userAgent')
    if not ua:
        return
    target = ''
    if 'Linux x86_64' in ua:
        target = 'Linux x86_64'
    elif 'Linux i686' in ua:
        target = 'Linux i686'
    if not target:
        return
    if config.get('navigator.platform') != target:
        config['navigator.platform'] = target
    if config.get('navigator.oscpu') != target:
        config['navigator.oscpu'] = target


def fix_screen_no_taskbar(config: Dict[str, Any], target_os: str) -> None:
    """Ensure screen.availHeight < screen.height so CreepJS's noTaskbar flag
    (screen.height == availHeight and screen.width == availWidth) doesn't flip.

    Every desktop OS keeps some chrome visible (Mac menu bar ~25px, Win taskbar
    ~40px, Linux panel ~27px); the BrowserForge pool occasionally ships
    fingerprints with identical screen/avail values which leak as a headless
    tell. Also clamp window.outerHeight (and innerHeight) to the new avail so
    the window isn't taller than the available area.
    """
    sw = config.get('screen.width')
    sh = config.get('screen.height')
    aw = config.get('screen.availWidth')
    ah = config.get('screen.availHeight')
    if not (sw and sh and aw == sw and ah == sh):
        return
    taskbar = 40 if target_os == 'win' else 25 if target_os == 'mac' else 27
    new_avail = sh - taskbar
    config['screen.availHeight'] = new_avail
    oh = config.get('window.outerHeight')
    if oh and oh > new_avail:
        ih = config.get('window.innerHeight')
        chrome = oh - ih if ih else 0
        config['window.outerHeight'] = new_avail
        if ih:
            config['window.innerHeight'] = new_avail - chrome


def clamp_window_dimensions(config: Dict[str, Any]) -> None:
    """Enforce inner <= outer <= avail <= screen on BOTH axes.

    The browser faithfully reports whatever we inject, so a BrowserForge
    fingerprint that ships e.g. outerWidth > screen.width or innerWidth >
    outerWidth leaks as an impossible geometry. Shrink each level down to its
    container, preserving the chrome delta between outer and inner where
    possible. Complements fix_screen_no_taskbar (which only clamps height).
    """
    for axis in ('Width', 'Height'):
        screen = config.get(f'screen.{axis.lower()}')
        avail = config.get(f'screen.avail{axis}')
        outer = config.get(f'window.outer{axis}')
        inner = config.get(f'window.inner{axis}')

        # avail must not exceed screen
        if screen and avail and avail > screen:
            config[f'screen.avail{axis}'] = screen
        avail_clamped = config.get(f'screen.avail{axis}', screen)

        # outer must not exceed avail (or screen if avail is unknown)
        outer_cap = avail_clamped if avail_clamped is not None else screen
        if outer and outer_cap and outer > outer_cap:
            chrome = max(0, outer - inner) if inner else 0
            config[f'window.outer{axis}'] = outer_cap
            if inner:
                config[f'window.inner{axis}'] = max(1, outer_cap - chrome)

        # inner must not exceed outer
        outer_clamped = config.get(f'window.outer{axis}', outer)
        inner_now = config.get(f'window.inner{axis}')
        if inner_now and outer_clamped and inner_now > outer_clamped:
            config[f'window.inner{axis}'] = outer_clamped


def clamp_screen_to_display(
    config: Dict[str, Any],
    max_width: Optional[int],
    max_height: Optional[int],
) -> None:
    """Shrink screen.width/height down to the bounds of the real display.

    BrowserForge takes a Screen constraint but drops it silently whenever it
    filters the fingerprint pool too far: FingerprintGenerator.partial_csp
    swallows the resulting failure and deletes the constraint unless strict=True.
    So the bound from get_screen_cons() is best-effort only, and a 1366x768
    laptop routinely gets a 2560x1440 fingerprint. browser-init.patch resizes the
    real chrome window to window.outerWidth/outerHeight, so an unbounded value
    renders past the edge of the monitor (daijro/camoufox#499).

    Keeps the taskbar delta (screen - avail) intact so fix_screen_no_taskbar's
    invariant survives. Callers must run clamp_window_dimensions afterwards to
    cascade the new bounds down to avail/outer/inner.
    """
    for axis, cap in (('width', max_width), ('height', max_height)):
        screen = config.get(f'screen.{axis}')
        if not (screen and cap) or screen <= cap:
            continue
        avail_key = 'screen.availWidth' if axis == 'width' else 'screen.availHeight'
        avail = config.get(avail_key)
        config[f'screen.{axis}'] = cap
        if avail:
            config[avail_key] = max(1, cap - max(0, screen - avail))


def clamp_window_position(config: Dict[str, Any]) -> None:
    """Keep the window box inside the screen: 0 <= screenX/Y <= screen - outer.

    BrowserForge's screenX/screenY are consistent with the screen it generated
    them against, so clamp_screen_to_display invalidates them. A window
    positioned partly off its own reported screen is an impossible geometry.
    """
    for axis, pos_key in (('Width', 'window.screenX'), ('Height', 'window.screenY')):
        screen = config.get(f'screen.{axis.lower()}')
        outer = config.get(f'window.outer{axis}')
        pos = config.get(pos_key)
        if pos is None or not (screen and outer):
            continue
        config[pos_key] = max(0, min(pos, screen - outer))


def set_media_devices_defaults(config: Dict[str, Any]) -> None:
    """Give the identity a plausible set of media devices.

    The patched media backend (media-device-spoofing.patch) enumerates and
    captures exactly the devices described by mediaDevices:{enabled, micros,
    webcams, speakers} and the aligned mediaDevices:{microphone,webcam,
    speaker}{Labels,Groups} lists, and Firefox's own pre-/post-grant exposure
    rules apply to them. Nothing is drawn when the caller already set any
    mediaDevices: key.
    """
    if any(k.startswith('mediaDevices:') for k in config):
        return
    # Before any getUserMedia grant Firefox exposes at most ONE device per
    # input kind and no audiooutput; after a grant it lists every device with
    # the OS's own labels ("Microphone Array (Realtek(R) Audio)", "MacBook Pro
    # Microphone", "Built-in Audio Analog Stereo"...). Draw a whole machine's
    # worth from the common desktop population for the claimed OS
    # (media-devices.json), seeded by the identity so the same identity
    # always reports the same devices. The browser applies the stock
    # pre-/post-grant exposure rules to this list.
    plat = str(config.get('navigator.platform', ''))
    if plat.startswith('Win'):
        os_key = 'win'
    elif plat.startswith('Mac'):
        os_key = 'mac'
    else:
        os_key = 'lin'
    config.update(draw_media_devices(os_key, identity_seed(config)))


_MEDIA_DEVICES_CACHE: Optional[Dict[str, Any]] = None


def _load_media_devices() -> Dict[str, Any]:
    """Per-OS catalogue of common sound cards / headsets / display audio /
    cameras with their post-grant labels (media-devices.json)."""
    global _MEDIA_DEVICES_CACHE
    if _MEDIA_DEVICES_CACHE is None:
        path = os.path.join(os.path.dirname(__file__), 'media-devices.json')
        with open(path, 'rb') as f:
            _MEDIA_DEVICES_CACHE = json.loads(f.read())
    return _MEDIA_DEVICES_CACHE


def _weighted_choice(rng: Random, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = float(sum(item.get('w', 1) for item in items))
    r = rng.random() * total
    for item in items:
        r -= item.get('w', 1)
        if r < 0:
            return item
    return items[-1]


# Share of machines with no microphone at all (a desktop tower with only a
# line-out) and with a built-in camera, per OS. macOS is modelled by the
# machine line itself (Mac mini / Mac Studio have neither).
_MEDIA_P_NO_MIC = {'win': 0.08, 'mac': 0.0, 'lin': 0.20}
_MEDIA_P_BUILTIN_CAM = {'win': 0.78, 'mac': 0.0, 'lin': 0.45}


def draw_media_devices(os_key: str, seed: Optional[int]) -> Dict[str, Any]:
    """Draw one machine's media devices for `os_key` ('win'|'mac'|'lin').

    Returns the mediaDevices:* config keys: counts plus aligned label and
    group lists. Devices of one piece of hardware (a sound card's microphone
    and speakers, a webcam and its microphone) share a group, as their
    groupId does on a real machine; Linux additionally lists the PulseAudio
    "Monitor of ..." source of every output as a microphone, as Firefox does.
    """
    rng = _rng(seed)
    cat = _load_media_devices().get(os_key) or _load_media_devices()['win']
    mics: List[Tuple[str, str]] = []
    outs: List[Tuple[str, str]] = []
    cams: List[Tuple[str, str]] = []
    counter = [0]

    def group() -> str:
        counter[0] += 1
        return f'hw-{counter[0]}'

    def add(item: Dict[str, Any], grp: str) -> None:
        for m in item.get('mics', []):
            mics.append((m, grp))
        for o in item.get('outs', []):
            outs.append((o, grp))
        if item.get('cam'):
            cams.append((item['cam'], group()))

    # 1. the machine's own sound card (+ built-in camera on macOS models)
    card = _weighted_choice(rng, cat['cards'])
    no_mic = rng.random() < _MEDIA_P_NO_MIC.get(os_key, 0.0)
    card_grp = group()
    if no_mic:
        add({**card, 'mics': []}, card_grp)
    else:
        add(card, card_grp)
    # 2. a built-in laptop camera (Windows/Linux); rare on a mic-less tower
    p_cam = _MEDIA_P_BUILTIN_CAM.get(os_key, 0.0)
    if rng.random() < (p_cam * 0.3 if no_mic else p_cam):
        builtin = [c for c in cat['cameras'] if not c.get('mic')]
        if builtin:
            cams.append((_weighted_choice(rng, builtin)['cam'], group()))
    # 3. a headset / USB microphone
    if rng.random() < cat.get('p_headset', 0.0):
        add(_weighted_choice(rng, cat['headsets']), group())
    # 4. display audio (HDMI/DP) -- occasionally a display with mic + camera
    if rng.random() < cat.get('p_display', 0.0):
        add(_weighted_choice(rng, cat['displays']), group())
    # 5. an external webcam, usually with its own microphone
    if rng.random() < cat.get('p_extra_camera', 0.0):
        external = [c for c in cat['cameras'] if c.get('mic')] or cat['cameras']
        cam = _weighted_choice(rng, external)
        grp = group()
        cams.append((cam['cam'], grp))
        if cam.get('mic'):
            mics.append((cam['mic'], grp))
    # 6. PulseAudio exposes a monitor source per output as a capture device
    if cat.get('monitor_sources'):
        for label, grp in list(outs):
            mics.append((f'Monitor of {label}', grp))

    return {
        'mediaDevices:enabled': True,
        'mediaDevices:micros': len(mics),
        'mediaDevices:webcams': len(cams),
        'mediaDevices:speakers': len(outs),
        'mediaDevices:microphoneLabels': [m for m, _ in mics],
        'mediaDevices:microphoneGroups': [g for _, g in mics],
        'mediaDevices:webcamLabels': [c for c, _ in cams],
        'mediaDevices:webcamGroups': [g for _, g in cams],
        'mediaDevices:speakerLabels': [o for o, _ in outs],
        'mediaDevices:speakerGroups': [g for _, g in outs],
    }


# -- WebGL <-> screen coherence (#729) ---------------------------------------
#
# BrowserForge picks navigator/screen; the GPU is drawn separately from
# webgl_data.db weighted only by OS. Nothing ties the two together, so the
# synthetic path can emit pairs no real machine ships -- a discrete GPU behind
# a 1024x600 netbook panel. Consistency checks (Pixelscan, Fingerprint.com)
# read that as masking even when every individual value is plausible alone.
#
# What can honestly be claimed here is narrow, because Firefox never reports
# the GPU it actually sees. dom/canvas/SanitizeRenderer.cpp collapses every
# renderer string into one of ~11 representative device buckets before a page
# sees it (prefs webgl.sanitize-unmasked-renderer and
# webgl.enable-renderer-query, both default true; resistFingerprinting
# replaces the value with "Mozilla" outright). That file's own header comment
# gives the flavour: `"GeForce RTX 3090" => "GeForce GTX 980"`. So every RTX,
# every Quadro M/P/V/T and every GeForce 900-7999 arrive as one string, while
# "Intel(R) UHD Graphics 620" and "Mesa Intel(R) Iris(R) Xe Graphics" both
# arrive as an "Intel(R) HD Graphics" spelling.
#
# Two consequences. Matching on raw model names -- RTX, Quadro, RX, UHD, Iris,
# Mesa Intel -- can never fire, because those are exactly the strings Gecko
# collapses away. And a bucket spanning a desktop RTX 4090 and a mobile GTX
# 1650 Max-Q carries no useful screen floor: 1366x768 laptops with discrete
# NVIDIA GPUs are ordinary hardware, not a tell.
#
# So the rule below holds only what is true of *every* part behind a bucket,
# and renderers are reduced to their bucket first (see _renderer_bucket) so
# the ANGLE, nouveau and /PCIe/SSE2 spellings of one GPU land on one rule
# instead of three different ones.

# Software rasterizers. A VM or headless host reports whatever resolution the
# window manager hands it, so no screen constrains them -- and the sampler
# must never come to *prefer* them, because a software renderer is a far
# stronger "this is a bot" signal than any GPU/screen mismatch.
_SOFTWARE_RENDERERS: Tuple[str, ...] = (
    'llvmpipe',
    'Microsoft Basic Render Driver',
    'SwiftShader',
    'Generic Renderer',
)

# Discrete NVIDIA, plus the AMD R5/R7/R9/RX/Vega bucket. Everything else in
# webgl_data.db reaches down into netbook territory and gets no floor at all:
# the "Intel(R) HD Graphics" bucket swallows the GMA 3150 netbook chipset,
# "Radeon HD 3200 Graphics" is Gecko's catch-all for a bare "AMD"/"Radeon"
# (the C-50/E-350 netbook APUs included), and Apple silicon drives arbitrary
# external monitors from a Mac mini or Mac Studio.
_DISCRETE_GPU_BUCKETS: FrozenSet[str] = frozenset(
    {
        'GeForce 8800 GTX',
        'GeForce GTX 480',
        'GeForce GTX 980',
        'Radeon R9 200 Series',
    }
)

# Discrete GPUs did not ship in netbooks, and netbook panels topped out at
# 1024x600. That is the whole of the claim.
#
# It is an area rather than a width x height pair because real panels do not
# dominate one another: 1280x800 and 1366x768 are both ordinary laptop
# screens, and a per-axis floor taken from either one rejects the other. A
# 1366x768 laptop with a discrete GPU is common hardware, not a tell.
_NETBOOK_MAX_PIXELS = 1024 * 600

# The three shapes SanitizeRenderer wraps a device bucket in.
_ANGLE_D3D_RE = re.compile(r'^ANGLE \([^,]*, (.*?) Direct3D.*\)$')
_ANGLE_VULKAN_RE = re.compile(r'^ANGLE \((.*)\) on Vulkan$')
_PCIE_SSE2_RE = re.compile(r'^(.*)/PCIe?/SSE2$')


def _renderer_bucket(renderer: str) -> str:
    """Reduce a reported renderer to Gecko's sanitized device bucket.

    "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or
    similar" (Windows), "NVIDIA GeForce GTX 980/PCIe/SSE2" (Linux proprietary
    driver) and "GeForce GTX 980, or similar" (nouveau, which loses the vendor
    prefix) are one GPU class in three spellings. Without this they land on
    three different rules, or none.
    """
    core = renderer.removesuffix(', or similar')
    match = _ANGLE_D3D_RE.match(core) or _ANGLE_VULKAN_RE.match(core)
    if match:
        core = match.group(1)
    match = _PCIE_SSE2_RE.match(core)
    if match:
        core = match.group(1)
    # SanitizeRenderer re-adds the "NVIDIA " prefix only when the raw string
    # carried it, so one bucket arrives both with and without it.
    return core.removeprefix('NVIDIA ')


# The smallest screen mainstream hardware still ships. BrowserForge's pool
# carries netbook-era geometry that essentially no 2026 device reports, and
# that is a tell on its own, whatever GPU sits behind it.
MODERN_SCREEN_FLOOR: Tuple[int, int] = (1366, 768)


def raise_screen_to_modern_floor(config: Dict[str, Any]) -> None:
    """Lift netbook-era screen geometry to something current hardware reports.

    BrowserForge still draws 1024x600 and friends. Those panels left
    production a decade and a half ago, so the screen is what has to move --
    no GPU choice makes that profile look current.

    Keeps the screen-to-avail gap intact so fix_screen_no_taskbar's invariant
    survives; the window box is reconciled by clamp_window_dimensions and
    clamp_window_position, which run after this. Call BEFORE
    clamp_screen_to_display so a genuinely small real monitor still wins.
    """
    min_w, min_h = MODERN_SCREEN_FLOOR
    sw = config.get('screen.width')
    sh = config.get('screen.height')
    if not (sw and sh) or (sw >= min_w and sh >= min_h):
        return

    # Measure the gaps before mutating, or they get folded into themselves.
    aw = config.get('screen.availWidth')
    ah = config.get('screen.availHeight')
    gap_w = sw - aw if aw else None
    gap_h = sh - ah if ah else None

    new_w, new_h = max(sw, min_w), max(sh, min_h)
    config['screen.width'] = new_w
    config['screen.height'] = new_h
    if gap_w is not None:
        config['screen.availWidth'] = max(1, new_w - max(0, gap_w))
    if gap_h is not None:
        config['screen.availHeight'] = max(1, new_h - max(0, gap_h))


def is_software_renderer(renderer: Optional[str]) -> bool:
    """Whether `renderer` is a software rasterizer rather than real hardware."""
    return bool(renderer) and any(name in renderer for name in _SOFTWARE_RENDERERS)


def gpu_screen_is_plausible(
    renderer: Optional[str], width: Optional[int], height: Optional[int]
) -> bool:
    """Whether `renderer` is a GPU that plausibly drives a `width` x `height` screen.

    Unconstrained buckets and software rasterizers pass. The set only names
    buckets whose floor holds for every part behind them, so anything absent
    from it is genuinely unconstrained rather than merely unrecognised.
    """
    if not renderer or not width or not height:
        return True
    if is_software_renderer(renderer):
        return True
    if _renderer_bucket(renderer) not in _DISCRETE_GPU_BUCKETS:
        return True
    return width * height > _NETBOOK_MAX_PIXELS


def sample_webgl_for_screen(
    target_os: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    attempts: int = 32,
    seed: Optional[int] = None,
) -> Dict[str, str]:
    """Sample a WebGL profile that is coherent with the screen already chosen.

    Rejection sampling, so the GPU keeps webgl_data.db's real OS-weighted
    distribution -- we only drop draws that contradict the screen. The screen
    itself is left alone on purpose: it has already been reconciled with the
    real display and the window box (clamp_screen_to_display,
    fix_screen_no_taskbar, clamp_window_dimensions, clamp_window_position),
    and widening it here to flatter the GPU would push a headful window back
    off the monitor it is drawn on (#499).

    The first draw settles hardware-vs-software at the pool's natural rate and
    is never resampled once it lands on a rasterizer. Rejecting only hardware
    draws would renormalise the survivors onto llvmpipe / WARP / SwiftShader:
    on a small screen that turns a 1.5% software rate into a 40% one, trading
    a weak incoherence for the strongest VM/headless tell there is.

    Falls back to that first draw when the pool holds nothing coherent, so an
    unusual screen degrades to today's behaviour rather than raising.
    """
    # A software rasteriser (llvmpipe / SwiftShader / WARP) as the presented
    # GPU is what every consumer-hardware check flags first ("no consumer
    # machine reports llvmpipe" -- sundial, measured 2026-09-14), so the draw
    # never settles on one: keep drawing until a hardware renderer that fits
    # the screen comes up, and only fall back to the first draw if the pool
    # holds nothing better.
    first = sample_webgl(target_os, seed=seed)
    renderer = first.get('webGl:renderer')
    if not is_software_renderer(renderer) and gpu_screen_is_plausible(renderer, width, height):
        return first

    fallback = None if is_software_renderer(renderer) else first
    for attempt in range(attempts - 1):
        candidate = sample_webgl(target_os, seed=None if seed is None else seed + 1 + attempt)
        renderer = candidate.get('webGl:renderer')
        if is_software_renderer(renderer):
            continue
        if gpu_screen_is_plausible(renderer, width, height):
            return candidate
        fallback = fallback or candidate
    return fallback or first


def _select_presets_file(ff_version: Optional[Any] = None) -> Path:
    """Pick the bundled-presets file appropriate for a given Firefox version.

    For Firefox >= PRESETS_V150_MIN_FF, prefer the v150 bundle (real
    fingerprints scraped from contemporary browsers); otherwise fall back to
    the original bundle.
    """
    try:
        major = int(str(ff_version).split('.', 1)[0]) if ff_version else 0
    except (ValueError, TypeError):
        major = 0
    if major >= PRESETS_V150_MIN_FF and PRESETS_V150_FILE.exists():
        return PRESETS_V150_FILE
    return PRESETS_FILE


def load_presets(ff_version: Optional[Any] = None) -> Optional[Dict]:
    """Load bundled fingerprint presets from JSON file."""
    path = _select_presets_file(ff_version)
    if path in _PRESETS_CACHE:
        return _PRESETS_CACHE[path]
    if not path.exists():
        return None
    with open(path) as f:
        _PRESETS_CACHE[path] = json.load(f)
    return _PRESETS_CACHE[path]


# Map OS names to preset keys
_OS_TO_PRESET_KEY = {
    'windows': 'windows',
    'macos': 'macos',
    'linux': 'linux',
    'win': 'windows',
    'mac': 'macos',
    'lin': 'linux',
}


def get_random_preset(
    os: Optional[str] = None,
    ff_version: Optional[Any] = None,
) -> Optional[Dict]:
    """
    Get a random preset for the given OS.
    Returns None if no presets are available.
    """
    presets = load_presets(ff_version)
    if not presets:
        return None

    all_os_keys = ['macos', 'windows', 'linux']

    if os:
        # Normalize OS name
        if isinstance(os, (list, tuple)):
            os_keys = [_OS_TO_PRESET_KEY.get(o, o) for o in os]
        else:
            os_keys = [_OS_TO_PRESET_KEY.get(os, os)]
    else:
        os_keys = all_os_keys

    # Collect all matching presets
    candidates: List[Dict] = []
    for key in os_keys:
        candidates.extend(presets.get('presets', {}).get(key, []))

    if not candidates:
        return None

    return choice(candidates)  # nosec


# Tokens that name the machine rather than the platform: Firefox leaves every one
# of them out of appVersion.
_APP_VERSION_DROPPED = ('Win64', 'x64', 'Mobile', 'Tablet')


def _app_version_from_user_agent(user_agent: str) -> Optional[str]:
    """The appVersion Firefox reports for a browser sending this user agent.

    "5.0 (<OS tokens>)": the parenthesised part of the UA without the
    architecture, the Gecko revision, or the Windows build number.
    """
    block = re.match(r'Mozilla/5\.0 \(([^)]*)\)', user_agent or '')
    if not block:
        return None
    kept = []
    for token in (part.strip() for part in block.group(1).split(';')):
        if (
            token.startswith('rv:')
            or token in _APP_VERSION_DROPPED
            or token.startswith('Linux ')
            or token.startswith('Intel Mac OS X')
        ):
            continue
        kept.append('Windows' if token.startswith('Windows') else token)
    return f"5.0 ({'; '.join(kept)})" if kept else None


def from_preset(preset: Dict, ff_version: Optional[str] = None) -> Dict[str, Any]:
    """
    Convert a real fingerprint preset to CAMOU_CONFIG format.
    """
    config: Dict[str, Any] = {}

    nav = preset.get('navigator', {})
    if nav.get('userAgent'):
        ua = nav['userAgent']
        # Replace Firefox version in UA if ff_version is provided
        if ff_version:
            ua = re.sub(r'Firefox/\d+\.0', f'Firefox/{ff_version}.0', ua)
            ua = re.sub(r'rv:\d+\.0', f'rv:{ff_version}.0', ua)
        config['navigator.userAgent'] = ua
    if nav.get('platform'):
        config['navigator.platform'] = nav['platform']
    if nav.get('hardwareConcurrency'):
        config['navigator.hardwareConcurrency'] = nav['hardwareConcurrency']
    if nav.get('oscpu'):
        config['navigator.oscpu'] = nav['oscpu']
    elif nav.get('platform'):
        # Derive oscpu from platform when not explicitly in the preset
        plat = nav['platform']
        if plat == 'MacIntel':
            config['navigator.oscpu'] = 'Intel Mac OS X 10.15'
        elif plat == 'Win32':
            config['navigator.oscpu'] = 'Windows NT 10.0; Win64; x64'
        elif 'Linux' in plat or 'linux' in plat:
            config['navigator.oscpu'] = 'Linux x86_64'
    if nav.get('appVersion'):
        config['navigator.appVersion'] = nav['appVersion']
    elif config.get('navigator.userAgent'):
        # Left unset, appVersion falls through to the *host's* value and then
        # contradicts the userAgent and platform set above: a Linux preset on a
        # macOS host reported "5.0 (Macintosh)" beside platform "Linux x86_64",
        # which any page can read in two properties.
        #
        # Firefox builds it from the same OS tokens as the userAgent, minus the
        # architecture and rv, with Windows collapsed to its family name. Deriving
        # it from the UA rather than from the platform keeps the distro token that
        # 20 of the bundled Linux presets carry ("X11; Ubuntu"), which a platform
        # lookup would flatten to "X11" — a mismatch of the same kind, if a
        # smaller one. Checked against 800 browserforge fingerprints: exact every
        # time.
        derived = _app_version_from_user_agent(config['navigator.userAgent'])
        if derived:
            config['navigator.appVersion'] = derived
    if 'maxTouchPoints' in nav:
        config['navigator.maxTouchPoints'] = nav['maxTouchPoints']

    screen = preset.get('screen', {})
    if screen.get('width'):
        config['screen.width'] = screen['width']
    if screen.get('height'):
        config['screen.height'] = screen['height']
    if screen.get('colorDepth'):
        config['screen.colorDepth'] = screen['colorDepth']
        config['screen.pixelDepth'] = screen['colorDepth']
    if screen.get('availWidth'):
        config['screen.availWidth'] = screen['availWidth']
    if screen.get('availHeight'):
        config['screen.availHeight'] = screen['availHeight']

    webgl = preset.get('webgl', {})
    if webgl.get('unmaskedVendor'):
        config['webGl:vendor'] = webgl['unmaskedVendor']
    if webgl.get('unmaskedRenderer'):
        config['webGl:renderer'] = webgl['unmaskedRenderer']

    # Generate unique random seeds per launch (1 to 2^32-1, excluding 0 which is a no-op in C++)
    # fonts:spacing_seed stays 0 (off): glyph-advance perturbation produces text
    # widths no real machine emits (see launch_options in utils.py).
    config['fonts:spacing_seed'] = 0
    config['audio:seed'] = randint(1, 4_294_967_295)  # nosec
    config['canvas:seed'] = randint(1, 4_294_967_295)  # nosec

    if preset.get('timezone'):
        config['timezone'] = preset['timezone']

    # Generate a unique random font subset from the OS font list.
    plat = nav.get('platform', '')
    if plat == 'MacIntel':
        target_os = 'macos'
    elif plat == 'Win32':
        target_os = 'windows'
    elif 'Linux' in plat or 'linux' in plat:
        target_os = 'linux'
    else:
        target_os = 'macos'
    try:
        config['fonts'] = _generate_random_font_subset(target_os, seed=identity_seed(config))
    except Exception:
        # Fallback to preset fonts if font generation fails
        if preset.get('fonts'):
            fonts = list(preset['fonts'])
            _ensure_marker_fonts(fonts, {
                'macos': _MACOS_MARKER_FONTS,
                'windows': _WINDOWS_MARKER_FONTS,
                'linux': _LINUX_MARKER_FONTS,
            }.get(target_os, _MACOS_MARKER_FONTS))
            config['fonts'] = fonts
    # Generate a unique random voice subset from the OS voice list
    try:
        config['voices'] = _generate_random_voice_subset(target_os, seed=identity_seed(config))
    except Exception:
        if preset.get('speechVoices'):
            config['voices'] = _normalize_preset_voices(
                preset['speechVoices'], target_os
            )

    return config


def _build_init_script(values: Dict[str, Any]) -> str:
    """
    Builds the JavaScript init script that calls per-context window.setXxx() functions.
    These functions self-destruct after first call, so they must run via addInitScript.
    """
    import json as _json

    lines = ['(function(v) {', '  var w = window;']

    setters = [
        ('fontSpacingSeed', 'setFontSpacingSeed', '{val}'),
        ('audioFingerprintSeed', 'setAudioFingerprintSeed', '{val}'),
        ('canvasSeed', 'setCanvasSeed', '{val}'),
        ('navigatorPlatform', 'setNavigatorPlatform', '{val}'),
        ('navigatorOscpu', 'setNavigatorOscpu', '{val}'),
        ('navigatorUserAgent', 'setNavigatorUserAgent', '{val}'),
        ('hardwareConcurrency', 'setNavigatorHardwareConcurrency', '{val}'),
        ('webglVendor', 'setWebGLVendor', '{val}'),
        ('webglRenderer', 'setWebGLRenderer', '{val}'),
    ]

    for key, fn_name, _template in setters:
        val = values.get(key)
        if val is not None:
            js_val = _json.dumps(val)
            lines.append(
                f'  if (typeof w.{fn_name} === "function") w.{fn_name}({js_val});'
            )

    # Screen dimensions (requires width + height together)
    sw = values.get('screenWidth')
    sh = values.get('screenHeight')
    if sw and sh:
        lines.append(
            f'  if (typeof w.setScreenDimensions === "function") w.setScreenDimensions({sw}, {sh});'
        )
        scd = values.get('screenColorDepth')
        if scd:
            lines.append(
                f'  if (typeof w.setScreenColorDepth === "function") w.setScreenColorDepth({scd});'
            )

    # Timezone — only call setTimezone() when we have an explicit value.
    # Without this, the C++ MaskConfig fallback (from CAMOU_CONFIG set by geoip
    # in launch_options) handles timezone for both main thread and workers via
    # SetNewDocument() and TimezoneManager::GetTimezone().
    # The old fallback read system TZ and poisoned RoverfoxStorageManager,
    # preventing MaskConfig from ever being consulted.
    tz = values.get('timezone')
    if tz:
        lines.append(
            f'  if (typeof w.setTimezone === "function") w.setTimezone({_json.dumps(tz)});'
        )

    # WebRTC IP
    ip = values.get('webrtcIP')
    if ip:
        lines.append(
            f'  if (typeof w.setWebRTCIPv4 === "function") w.setWebRTCIPv4({_json.dumps(ip)});'
        )
    else:
        lines.append(
            '  if (typeof w.setWebRTCIPv4 === "function") w.setWebRTCIPv4("");'
        )

    # Font list (comma-separated)
    font_list = values.get('fontList')
    if font_list and len(font_list) > 0:
        joined = ','.join(font_list)
        lines.append(
            f'  if (typeof w.setFontList === "function") w.setFontList({_json.dumps(joined)});'
        )

    # Speech voices (comma-separated names). config['voices'] holds MaskConfig
    # voice objects; extract the display name from each (tolerating a legacy
    # list of plain name strings).
    voices = values.get('speechVoices')
    if voices and len(voices) > 0:
        names = [v['name'] if isinstance(v, dict) else v for v in voices]
        joined = ','.join(names)
        lines.append(
            f'  if (typeof w.setSpeechVoices === "function") w.setSpeechVoices({_json.dumps(joined)});'
        )

    lines.append('})();')
    return '\n'.join(lines)


def generate_context_fingerprint(
    preset: Optional[Dict] = None,
    os: Optional[str] = None,
    ff_version: Optional[str] = None,
    webrtc_ip: Optional[str] = None,
    timezone: Optional[str] = None,
    locale: Optional[str] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate fingerprint values for a single per-context identity.
    Returns a dict with init_script (JS string) and context_options (Playwright options).

    By default, uses BrowserForge for infinite unique synthetic fingerprints.
    Pass a preset dict to use a real fingerprint preset instead.

    Parameters:
        timezone: IANA timezone string (e.g. 'Europe/London'). When provided,
            injected into config before init_script generation. Takes priority
            over any timezone from the preset.
        locale: BCP-47 locale string (e.g. 'en-GB'). When provided, parsed via
            normalize_locale() and injected into config. Also sets
            context_options['locale'] for Playwright.
        config_overrides: Dict of CAMOU_CONFIG keys to override after config
            is built but before init_script is rendered. Useful for disabling
            perturbation (e.g. {'fonts:spacing_seed': 0}).
    """
    if preset is not None:
        # Use real fingerprint preset
        config = from_preset(preset, ff_version)
        nav = preset.get('navigator', {})
        screen = preset.get('screen', {})
        webgl = preset.get('webgl', {})
    else:
        # Fall back to BrowserForge synthetic generation
        fp = generate_fingerprint(os=os)
        config = from_browserforge(fp, ff_version)

        # Add seeds (BrowserForge doesn't generate these)
        config.setdefault('fonts:spacing_seed', 0)  # perturbation off; see utils.launch_options
        config.setdefault('audio:seed', randint(1, 4_294_967_295))  # nosec
        config.setdefault('canvas:seed', randint(1, 4_294_967_295))  # nosec

        # Determine target OS from platform for font/voice generation
        plat = config.get('navigator.platform', '')
        os_name = 'macos'
        if plat == 'Win32':
            os_name = 'windows'
        elif 'Linux' in plat or 'linux' in plat:
            os_name = 'linux'

        # Add fonts (BrowserForge doesn't generate these)
        if 'fonts' not in config:
            try:
                config['fonts'] = _generate_random_font_subset(os_name, seed=identity_seed(config))
            except Exception:
                pass

        # Add voices (BrowserForge doesn't generate these)
        if 'voices' not in config:
            try:
                config['voices'] = _generate_random_voice_subset(os_name, seed=identity_seed(config))
            except Exception:
                pass

        # Derive oscpu if BrowserForge didn't provide it
        if 'navigator.oscpu' not in config:
            plat = config.get('navigator.platform', '')
            if plat == 'MacIntel':
                config['navigator.oscpu'] = 'Intel Mac OS X 10.15'
            elif plat == 'Win32':
                config['navigator.oscpu'] = 'Windows NT 10.0; Win64; x64'
            elif 'Linux' in plat or 'linux' in plat:
                config['navigator.oscpu'] = 'Linux x86_64'

        # Sample WebGL vendor/renderer from database (BrowserForge doesn't generate these)
        if not config.get('webGl:vendor') or not config.get('webGl:renderer'):
            _os_map = {'macos': 'mac', 'linux': 'lin', 'windows': 'win'}
            _target_os = _os_map.get(os or '', None)
            if not _target_os:
                plat = config.get('navigator.platform', '')
                if plat == 'Win32':
                    _target_os = 'win'
                elif 'Linux' in plat or 'linux' in plat:
                    _target_os = 'lin'
                else:
                    _target_os = 'mac'
            try:
                # Same coherence treatment launch_options applies (#729): lift
                # netbook geometry, then keep the GPU consistent with whatever
                # screen this identity ended up with. This path has no real
                # display to reconcile against, so the floor is unconditional.
                raise_screen_to_modern_floor(config)
                webgl_fp = sample_webgl_for_screen(
                    _target_os, config.get('screen.width'), config.get('screen.height')
                )
                webgl_fp.pop('webGl2Enabled', None)
                config.update(webgl_fp)
            except Exception:
                pass

        # Build source dicts from BrowserForge config for init_values
        nav = {
            'platform': config.get('navigator.platform'),
            'hardwareConcurrency': config.get('navigator.hardwareConcurrency'),
        }
        screen = {
            'width': config.get('screen.width'),
            'height': config.get('screen.height'),
            'colorDepth': config.get('screen.colorDepth'),
            'devicePixelRatio': None,
        }
        webgl = {
            'unmaskedVendor': config.get('webGl:vendor'),
            'unmaskedRenderer': config.get('webGl:renderer'),
        }
        preset = {'navigator': nav, 'screen': screen, 'webgl': webgl}

    # Inject explicit timezone/locale into config (takes priority over preset)
    if timezone:
        config['timezone'] = timezone
    if locale:
        from .locales import normalize_locale
        parsed = normalize_locale(locale)
        config['locale:language'] = parsed.language
        config['locale:region'] = parsed.region
        config['navigator.language'] = parsed.as_string
        if parsed.script:
            config['locale:script'] = parsed.script

    # Apply caller overrides before rendering init_script
    if config_overrides:
        config.update(config_overrides)

    # Build the values dict for the init script (works for both paths)
    init_values: Dict[str, Any] = {
        'fontSpacingSeed': config.get('fonts:spacing_seed'),
        'audioFingerprintSeed': config.get('audio:seed'),
        'canvasSeed': config.get('canvas:seed'),
        'navigatorPlatform': nav.get('platform'),
        'navigatorOscpu': config.get('navigator.oscpu'),
        'navigatorUserAgent': config.get('navigator.userAgent'),
        'hardwareConcurrency': nav.get('hardwareConcurrency') or config.get('navigator.hardwareConcurrency'),
        'webglVendor': webgl.get('unmaskedVendor'),
        'webglRenderer': webgl.get('unmaskedRenderer'),
        'screenWidth': screen.get('width'),
        'screenHeight': screen.get('height'),
        'screenColorDepth': screen.get('colorDepth'),
        'timezone': preset.get('timezone') if isinstance(preset.get('timezone'), str) else config.get('timezone'),
        'fontList': config.get('fonts'),
        'speechVoices': config.get('voices'),
        'webrtcIP': webrtc_ip or '',
    }

    init_script = _build_init_script(init_values)

    # Playwright context options that must be set at context creation
    context_options: Dict[str, Any] = {}
    ua = config.get('navigator.userAgent')
    if ua:
        context_options['user_agent'] = ua
    sw = screen.get('width')
    sh = screen.get('height')
    if sw and sh:
        context_options['viewport'] = {
            'width': sw,
            'height': max(sh - 28, 600),
        }
    dpr = screen.get('devicePixelRatio')
    if dpr:
        context_options['device_scale_factor'] = dpr
    tz = config.get('timezone')
    if not tz and isinstance(preset, dict):
        tz = preset.get('timezone')
    if tz:
        context_options['timezone_id'] = tz
    nav_lang = config.get('navigator.language')
    if nav_lang:
        context_options['locale'] = nav_lang

    return {
        'init_script': init_script,
        'context_options': context_options,
        'config': config,
        'preset': preset,
    }


@dataclass
class ExtendedScreen(ScreenFingerprint):
    """
    An extended version of Browserforge's ScreenFingerprint class
    """

    screenY: Optional[int] = None


def _cast_to_properties(
    camoufox_data: Dict[str, Any],
    cast_enum: Dict[str, Any],
    bf_dict: Dict[str, Any],
    ff_version: Optional[str] = None,
) -> None:
    """
    Casts Browserforge fingerprints to Camoufox config properties.
    """
    for key, data in bf_dict.items():
        # Ignore non-truthy values
        if not data:
            continue
        # Get the associated Camoufox property
        type_key = cast_enum.get(key)
        if not type_key:
            continue
        # If the value is a dictionary, recursively recall
        if isinstance(data, dict):
            _cast_to_properties(camoufox_data, type_key, data, ff_version)
            continue
        # Fix values that are out of bounds
        if type_key.startswith("screen.") and isinstance(data, int) and data < 0:
            data = 0
        # Replace the Firefox versions with ff_version
        if ff_version and isinstance(data, str):
            data = re.sub(r'(?<!\d)(1[0-9]{2})(\.0)(?!\d)', rf'{ff_version}\2', data)
        camoufox_data[type_key] = data


def handle_screenXY(camoufox_data: Dict[str, Any], fp_screen: ScreenFingerprint) -> None:
    """
    Helper method to set window.screenY based on Browserforge's screenX value.
    """
    # Skip if manually provided
    if 'window.screenY' in camoufox_data:
        return
    # Default screenX to 0 if not provided
    screenX = fp_screen.screenX
    if not screenX:
        camoufox_data['window.screenX'] = 0
        camoufox_data['window.screenY'] = 0
        return

    # If screenX is within [-50, 50], use the same value for screenY
    if screenX in range(-50, 51):
        camoufox_data['window.screenY'] = screenX
        return

    # Browserforge thinks the browser is windowed. # Randomly generate a screenY value.
    screenY = fp_screen.availHeight - fp_screen.outerHeight
    if screenY == 0:
        camoufox_data['window.screenY'] = 0
    elif screenY > 0:
        camoufox_data['window.screenY'] = randrange(0, screenY)  # nosec
    else:
        camoufox_data['window.screenY'] = randrange(screenY, 0)  # nosec


def from_browserforge(fingerprint: Fingerprint, ff_version: Optional[str] = None) -> Dict[str, Any]:
    """
    Converts a Browserforge fingerprint to a Camoufox config.
    """
    camoufox_data: Dict[str, Any] = {}
    _cast_to_properties(
        camoufox_data,
        cast_enum=BROWSERFORGE_DATA,
        bf_dict=asdict(fingerprint),
        ff_version=ff_version,
    )
    handle_screenXY(camoufox_data, fingerprint.screen)

    return camoufox_data


def handle_window_size(fp: Fingerprint, outer_width: int, outer_height: int) -> None:
    """
    Helper method to set a custom outer window size, and center it in the screen
    """
    # Cast the screen to an ExtendedScreen
    fp.screen = ExtendedScreen(**asdict(fp.screen))
    sc = fp.screen

    # Center the window on the screen
    sc.screenX += (sc.width - outer_width) // 2
    sc.screenY = (sc.height - outer_height) // 2

    # Update inner dimensions if set
    if sc.innerWidth:
        sc.innerWidth = max(outer_width - sc.outerWidth + sc.innerWidth, 0)
    if sc.innerHeight:
        sc.innerHeight = max(outer_height - sc.outerHeight + sc.innerHeight, 0)

    # Set outer dimensions
    sc.outerWidth = outer_width
    sc.outerHeight = outer_height


def generate_fingerprint(window: Optional[Tuple[int, int]] = None, **config) -> Fingerprint:
    """
    Generates a Firefox fingerprint with Browserforge.
    """
    if window:  # User-specified outer window size
        fingerprint = FP_GENERATOR.generate(**config)
        handle_window_size(fingerprint, *window)
        return fingerprint
    return FP_GENERATOR.generate(**config)


if __name__ == "__main__":
    from pprint import pprint

    fp = generate_fingerprint()
    pprint(from_browserforge(fp))
