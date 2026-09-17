import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import orjson

from camoufox.pkgman import OS_ARCH_MATRIX

# Get database path relative to this file
DB_PATH = Path(__file__).parent / 'webgl_data.db'

# Extensions a release Firefox never exposes (draft extensions behind
# webgl.enable-draft-extensions, or mobile-only): some database rows carry
# them, and a spoofed list that names one is a tell on its own. Measured on
# stock Firefox 152.0.4 on real Windows 11 (ANGLE D3D11) 2026-09-16: none of
# these four are exposed. WEBGL_provoking_vertex is NOT in this set: stock
# Firefox on Apple GPUs and on Windows does expose it.
_NEVER_EXPOSED_EXTENSIONS = frozenset(
    {
        'WEBGL_multi_draw',
        'WEBGL_clip_cull_distance',
        'EXT_texture_norm16',
        'WEBGL_compressed_texture_etc1',
    }
)

# OVR_multiview2 is a RELEASE extension whose availability depends on the
# graphics backend. On Windows, Firefox renders WebGL through ANGLE's D3D11
# backend, which implements multiview on every D3D11 GPU: stock 152.0.4 on
# a Windows 11 host exposes it on WebGL2 (MAX_VIEWS_OVR=4, headless and headed), and the
# recorded corpus has it on 13 of the 15 Windows rows with WebGL2 (never on
# WebGL1) -- filtering it there was a leak. On Linux it depends on
# the host GL driver (stock on an NVIDIA box does not expose it), and
# ClientWebGLContext::IsSupported answers from the spoofed list without asking
# the host, so a Linux identity could advertise an extension the GPU cannot
# back; it stays filtered off Windows.
_HOST_DEPENDENT_EXTENSIONS = frozenset({'OVR_multiview2'})


def _filtered_extensions(os: str) -> frozenset:
    if os == 'win':
        return _NEVER_EXPOSED_EXTENSIONS
    return _NEVER_EXPOSED_EXTENSIONS | _HOST_DEPENDENT_EXTENSIONS


def _load_webgl_data(data_str: str, os: str) -> Dict[str, str]:
    data = orjson.loads(data_str)
    blocked = _filtered_extensions(os)
    for key in ('webGl:supportedExtensions', 'webGl2:supportedExtensions'):
        exts = data.get(key)
        if isinstance(exts, list):
            data[key] = [e for e in exts if e not in blocked]
    return data


def sample_webgl(
    os: str, vendor: Optional[str] = None, renderer: Optional[str] = None, seed: Optional[int] = None
) -> Dict[str, str]:
    """
    Sample a random WebGL vendor/renderer combination and its data based on OS probabilities.
    Optionally use a specific vendor/renderer pair.

    Args:
        os: Operating system ('win', 'mac', or 'lin')
        vendor: Optional specific vendor to use
        renderer: Optional specific renderer to use (requires vendor to be set)

    Returns:
        Dict containing WebGL data including vendor, renderer and additional parameters

    Raises:
        ValueError: If invalid OS provided or no data found for OS/vendor/renderer
    """
    # Check that the OS is valid (avoid SQL injection)
    if os not in OS_ARCH_MATRIX:
        raise ValueError(f'Invalid OS: {os}. Must be one of: win, mac, lin')

    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if vendor and renderer:
        # Get specific vendor/renderer pair and verify it exists for this OS
        cursor.execute(
            f'SELECT vendor, renderer, data, {os} FROM webgl_fingerprints '  # nosec
            'WHERE vendor = ? AND renderer = ?',
            (vendor, renderer),
        )
        result = cursor.fetchone()

        if not result:
            raise ValueError(f'No WebGL data found for vendor "{vendor}" and renderer "{renderer}"')

        if result[3] <= 0:  # Check OS-specific probability
            # Get a list of possible (vendor, renderer) pairs for this OS
            cursor.execute(
                f'SELECT DISTINCT vendor, renderer FROM webgl_fingerprints WHERE {os} > 0'  # nosec
            )
            possible_pairs = cursor.fetchall()
            raise ValueError(
                f'Vendor "{vendor}" and renderer "{renderer}" combination not valid for {os.title()}.\n'
                f'Possible pairs: {", ".join(str(pair) for pair in possible_pairs)}'
            )

        conn.close()
        return _load_webgl_data(result[2], os)

    # Get all vendor/renderer pairs and their probabilities for this OS
    cursor.execute(
        f'SELECT vendor, renderer, data, {os} FROM webgl_fingerprints WHERE {os} > 0'  # nosec
    )
    results = cursor.fetchall()
    conn.close()

    if not results:
        raise ValueError(f'No WebGL data found for OS: {os}')

    # Drop pairs this OS cannot report before sampling. webgl_data.db weights
    # each pair per OS, and its macOS column carries a Braswell Atom IGP
    # ("Intel(R) HD Graphics 400") at 7.4% and a desktop PC card ("Radeon R9 200
    # Series") at 3.7% -- neither shipped in any Mac, so ~11% of macOS
    # identities were drawing a GPU that would contradict the rest of the
    # identity the moment a page read the renderer string beside the platform.
    # Filtering here rather than repairing later keeps reported and sampled
    # WebGL parameters from the same recorded device.
    from ..coherence import gpu_fits_os

    coherent = [row for row in results if gpu_fits_os(row[1], os)]
    if coherent:
        results = coherent

    # Split into separate arrays
    _, _, data_strs, probs = map(list, zip(*results))

    # Convert probabilities to numpy array and normalize
    probs_array = np.array(probs, dtype=np.float64)
    probs_array = probs_array / probs_array.sum()

    # Sample based on probabilities
    # Seeded so the same identity always draws the same device (#442/#765).
    idx = np.random.default_rng(seed).choice(len(probs_array), p=probs_array)

    # Parse the JSON data string
    return _load_webgl_data(data_strs[idx], os)


def get_possible_pairs() -> Dict[str, List[Tuple[str, str]]]:
    """
    Get all possible (vendor, renderer) pairs for all OS, where the probability is greater than 0.
    """
    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get all vendor/renderer pairs for each OS where probability > 0
    result: Dict[str, List[Tuple[str, str]]] = {}
    for os_type in OS_ARCH_MATRIX:
        cursor.execute(
            'SELECT DISTINCT vendor, renderer FROM webgl_fingerprints '
            f'WHERE {os_type} > 0 ORDER BY {os_type} DESC',  # nosec
        )
        result[os_type] = cursor.fetchall()

    conn.close()
    return result
