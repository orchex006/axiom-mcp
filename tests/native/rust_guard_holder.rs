//! Test-only holder using the pinned core's real native guard implementation.
use axiom_platform::guard::{GuardDir, GuardRole};
use graph_core::locks::LockMode;
use std::io::Write;
use std::time::{Duration, Instant};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let command = args.get(1).map(String::as_str).unwrap_or("");
    let value = |flag: &str, default: &str| -> String {
        args.iter()
            .position(|arg| arg == flag)
            .and_then(|index| args.get(index + 1))
            .cloned()
            .unwrap_or_else(|| default.to_string())
    };
    if !matches!(command, "hold" | "try") {
        std::process::exit(2);
    }
    let directory = GuardDir::at(value("--dir", ""));
    let names = value(
        if command == "hold" {
            "--locks"
        } else {
            "--lock"
        },
        "data.lock",
    );
    let mode = if value("--mode", "exclusive") == "shared" {
        LockMode::Shared
    } else {
        LockMode::Exclusive
    };
    let timeout = value("--timeout-ms", "5000").parse::<u64>().unwrap();
    let deadline = Instant::now() + Duration::from_millis(timeout);
    let mut held = Vec::new();
    for name in names.split(',') {
        let role = match name {
            "admission.lock" => GuardRole::Admission,
            "data.lock" => GuardRole::Data,
            _ => std::process::exit(2),
        };
        loop {
            match directory.try_acquire(role, mode) {
                Ok(handle) => {
                    held.push(handle);
                    break;
                }
                Err(_) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(20))
                }
                Err(_) => {
                    println!("{{\"event\":\"failed\",\"reason\":\"timeout\"}}");
                    std::process::exit(3);
                }
            }
        }
    }
    println!("{{\"event\":\"acquired\",\"participant\":\"rust\"}}");
    std::io::stdout().flush().unwrap();
    if command == "hold" {
        std::thread::sleep(Duration::from_millis(
            value("--hold-ms", "500").parse().unwrap(),
        ));
    }
    drop(held);
    println!("{{\"event\":\"released\"}}");
}
