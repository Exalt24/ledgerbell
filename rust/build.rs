//! Compiles proto/fleet.proto with protox, a protobuf compiler written in Rust, so building the crate
//! does not need `protoc` installed.

fn main() {
    println!("cargo:rerun-if-changed=proto/fleet.proto");
    let fds = protox::compile(["proto/fleet.proto"], ["proto"]).expect("fleet.proto compiles");
    prost_build::Config::new()
        .compile_fds(fds)
        .expect("generated code for fleet.proto");
}
