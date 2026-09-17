// Usage: oozcli <compressed_in> <uncompressed_size> <out_file>
// Decompresses a raw Oodle (Kraken/Mermaid/Selkie/Leviathan) block.
use std::io::Write;
fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 { eprintln!("usage: oozcli <in> <uncompressed_size> <out>"); std::process::exit(2); }
    let input = std::fs::read(&args[1]).expect("read input");
    let size: usize = args[2].parse().expect("size");
    let mut output = vec![0u8; size];
    let mut ex = oozextract::Extractor::new();
    ex.read_from_slice(&input, &mut output).expect("decompress");
    std::fs::File::create(&args[3]).expect("create").write_all(&output).expect("write");
    eprintln!("ok {} -> {} bytes", input.len(), size);
}
