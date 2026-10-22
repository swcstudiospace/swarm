use std::error::Error;

const PROTO_FILES: &[&str] = &["src/gsd.proto"];
const INCLUDE_DIRS: &[&str] = &["src"];

fn main() -> Result<(), Box<dyn Error>> {
    for proto in PROTO_FILES {
        println!("cargo:rerun-if-changed={proto}");
    }
    prost_build::Config::new().compile_protos(PROTO_FILES, INCLUDE_DIRS)?;
    Ok(())
}
