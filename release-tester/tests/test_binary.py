import pytest

from src.binary import BinaryNotFound, candidates, find_binary


def make(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n', encoding='utf-8')
    return path


def test_finds_the_linux_binary(tmp_path):
    make(tmp_path / 'camoufox-bin')
    make(tmp_path / 'camoufox')
    assert find_binary(tmp_path, 'linux').name == 'camoufox-bin'


def test_falls_back_to_camoufox_when_bin_is_absent(tmp_path):
    make(tmp_path / 'camoufox')
    assert find_binary(tmp_path, 'linux').name == 'camoufox'


def test_finds_the_macos_binary_inside_the_app_bundle(tmp_path):
    make(tmp_path / 'Camoufox.app' / 'Contents' / 'MacOS' / 'camoufox')
    found = find_binary(tmp_path, 'macos')
    assert found.parts[-3:] == ('Contents', 'MacOS', 'camoufox')


def test_finds_the_windows_exe(tmp_path):
    make(tmp_path / 'camoufox.exe')
    assert find_binary(tmp_path, 'windows').name == 'camoufox.exe'


def test_descends_one_level_into_a_nested_package(tmp_path):
    # actions/upload-artifact + unzip commonly leaves the package one directory
    # down rather than at the root the caller names.
    make(tmp_path / 'camoufox-152.0.4-beta.29-lin.x86_64' / 'camoufox-bin')
    assert find_binary(tmp_path, 'linux').name == 'camoufox-bin'


def test_raises_with_what_it_looked_for(tmp_path):
    with pytest.raises(BinaryNotFound) as excinfo:
        find_binary(tmp_path, 'linux')
    assert 'camoufox-bin' in str(excinfo.value)


def test_rejects_an_unknown_target():
    with pytest.raises(ValueError, match='unknown target'):
        candidates('solaris')


def test_a_directory_named_like_the_binary_is_not_a_match(tmp_path):
    # macOS ships Camoufox.app as a directory; a stray `camoufox` directory on
    # any platform must not be handed to Playwright as an executable.
    (tmp_path / 'camoufox-bin').mkdir()
    make(tmp_path / 'camoufox')
    assert find_binary(tmp_path, 'linux').name == 'camoufox'
