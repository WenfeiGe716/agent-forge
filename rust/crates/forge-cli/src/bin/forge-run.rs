use forge_cli::owned_run::{accept, execute, RunInput};
use forge_store::Store;
use serde_json::json;
use std::{fs::File, io::Read, path::PathBuf};

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() != 5
        || args[1] != "--db"
        || !PathBuf::from(&args[2]).is_absolute()
        || !matches!(
            (args[0].as_str(), args[3].as_str()),
            ("execute", "--input") | ("accept", "--run")
        )
    {
        eprintln!("usage: forge-run execute --db /private/kernel.sqlite3 --input /controlled/input.json\n       forge-run accept --db /private/kernel.sqlite3 --run RUN_ID");
        std::process::exit(2);
    }
    let result = async {
        let mut store = Store::open(&args[2])?;
        if args[0] == "accept" {
            return accept(&mut store, &args[4]);
        }
        let mut bytes = Vec::new();
        File::open(&args[4])?
            .take(1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() > 1024 * 1024 {
            return Err(forge_cli::owned_run::HostError::Input("input too large"));
        }
        let input: RunInput = serde_json::from_slice(&bytes)?;
        execute(&mut store, input).await
    }
    .await;
    match result {
        Ok(run) => println!(
            "{}",
            json!({"ok": true, "run_id": run.id, "owner": run.owner, "status": run.status, "version": run.version, "execution": run.execution})
        ),
        Err(error) => {
            eprintln!("{}", json!({"ok": false, "error": error.to_string()}));
            std::process::exit(1);
        }
    }
}
