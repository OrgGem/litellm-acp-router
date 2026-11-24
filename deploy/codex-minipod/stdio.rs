use sandbox_client::{ClientOutputKind, ControlEndpoint, SandboxClient};
use sandbox_core::{ExecRequest, ExecTarget, SandboxId};
use std::io::Read;
use std::time::Duration;
use tokio::io::AsyncWriteExt;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let (endpoint, id, action, slot) = match args.as_slice() {
        [endpoint, id, action] => (endpoint, id, action, None),
        [endpoint, id, action, slot] if action == "exec" => (endpoint, id, action, Some(slot)),
        _ => return Err("usage: minipod-stdio <endpoint> <sandbox-id> <load|exec> [slot]".into()),
    };
    if !matches!(action.as_str(), "load" | "exec") {
        return Err("invalid sandbox action".into());
    }
    let mut argv = vec![
        "/bin/sh".into(),
        "/workspace/minipod-codex/rootless.sh".into(),
        action.clone(),
    ];
    if let Some(slot) = slot {
        let parsed: u32 = slot.parse().map_err(|_| "invalid slot index")?;
        if parsed >= 16 || slot != &parsed.to_string() {
            return Err("slot index out of range".into());
        }
        argv.push(slot.clone());
    }
    let client = SandboxClient::new(ControlEndpoint::parse(endpoint)?);
    let req = ExecRequest {
        target: ExecTarget::Guest,
        argv,
        env: Default::default(),
        cwd: None,
        user: None,
        deadline: Duration::from_secs(if action == "load" { 300 } else { 900 }),
        stdout_budget_bytes: 16 * 1024 * 1024,
        stderr_budget_bytes: 256 * 1024,
    };
    let mut stream = client.exec_stream(&SandboxId::new(id), req).await?;
    let (sender, mut input) = tokio::sync::mpsc::channel(8);
    std::thread::spawn(move || {
        let mut stdin = std::io::stdin().lock();
        let mut chunk = [0; 65536];
        while let Ok(n) = stdin.read(&mut chunk) {
            if n == 0 || sender.blocking_send(chunk[..n].to_vec()).is_err() {
                break;
            }
        }
    });

    let mut stdin_open = true;
    let mut stdout = tokio::io::stdout();
    let mut stderr = tokio::io::stderr();
    loop {
        tokio::select! {
            chunk = input.recv(), if stdin_open => match chunk {
                Some(chunk) => stream.write_stdin(chunk).await?,
                None => { stdin_open = false; stream.close_stdin(); }
            },
            output = stream.next_output() => {
                let Some(output) = output else { break; };
                match output.stream {
                    ClientOutputKind::Stdout => { stdout.write_all(&output.data).await?; stdout.flush().await?; }
                    ClientOutputKind::Stderr => { stderr.write_all(&output.data).await?; stderr.flush().await?; }
                }
            }
        }
    }
    let exit = stream.wait_exit().await?;
    if exit.code != Some(0) || exit.timed_out || exit.stdout_truncated || exit.stderr_truncated {
        return Err(format!("sandbox process failed: {exit:?}").into());
    }
    Ok(())
}
