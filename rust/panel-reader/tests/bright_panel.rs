//! Regression: the panel over a pale map wall (ProxyRax sparring game, ~3:14). The wall's diagonal edge is light
//! grey (min channel ~165-185): it passed the segmentation mask, sat 7 px after "645" and read as "645?", so every
//! read from 1:14 to 4:40 was "panel unreadable". The dark crop from the same game must keep reading as before.

use panel_reader::{decode_png, Panel, Reader};
use std::path::{Path, PathBuf};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).to_path_buf()
}

fn reader() -> Reader {
    Reader::load(&root().join("../../live/glyphs2.json")).unwrap()
}

fn read(name: &str) -> (String, Option<Panel>) {
    let bytes = std::fs::read(root().join("tests/fixtures").join(name)).unwrap();
    let img = decode_png(&bytes).unwrap();
    let r = reader();
    (r.ocr_text(&img).unwrap(), r.read(&img).unwrap())
}

fn values(p: &Option<Panel>) -> Option<(u32, u32, u32, u32)> {
    p.as_ref().map(|p| (p.minerals, p.gas, p.used, p.cap))
}

#[test]
fn pale_wall_behind_panel_reads() {
    let (text, panel) = read("bright_wall_645_36_24-39.png");
    assert_eq!(text, "645 | 36 | 24/39");
    assert_eq!(values(&panel), Some((645, 36, 24, 39)));
}

#[test]
fn dark_background_unchanged() {
    let (text, panel) = read("dark_720_36_28-39.png");
    assert_eq!(text, "720 | 36 | 28/39");
    assert_eq!(values(&panel), Some((720, 36, 28, 39)));
}
