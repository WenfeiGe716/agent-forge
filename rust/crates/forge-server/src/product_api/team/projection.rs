//! Public projections deliberately select fields instead of serializing the
//! claim state or transport binding stored in a team's task specification.
use super::*;

pub(crate) fn tasks(c: &Connection, run: &str) -> Result<Vec<Value>> {
    let mut query=c.prepare("SELECT id,key,agent_id,kind,title,status,spec,result,started_at FROM tasks WHERE run_id=?1 ORDER BY started_at,key")?;
    let items=query.query_map([run],|r| {
        let spec=decode(r.get(6)?)?;
        let result=decode(r.get(7)?)?;
        Ok(crate::python_sqlite::redact(json!({
            "id":r.get::<_,String>(0)?,"key":r.get::<_,String>(1)?,"agent_id":r.get::<_,String>(2)?,
            "kind":r.get::<_,String>(3)?,"title":r.get::<_,String>(4)?,"status":r.get::<_,String>(5)?,
            "spec":{"assignment_type":spec["assignment_type"],"description":spec["description"],"acceptance":spec["acceptance"],"report_to":spec["report_to"],"source_task_key":spec["source_task_key"],"meeting_id":spec["meeting_id"]},
            "result":result,"summary":result["summary"],"started_at":r.get::<_,String>(8)?,"version":spec["claim_state"]["version"],"message_count":0
        })))
    })?.collect::<std::result::Result<Vec<_>,_>>()?;
    Ok(items)
}

pub(crate) fn directory(
    c: &Connection,
    snapshot: &Value,
    run: &str,
    member: Option<&str>,
) -> Result<Value> {
    Ok(crate::python_sqlite::redact(inspect(
        c, snapshot, run, member,
    )?))
}

/// Bind the exact host-observed turn to its active task. This is called after
/// turn/start succeeds; the caller never derives these IDs from model input.
pub(crate) fn bind_execution(
    c: &mut Connection,
    snapshot: &Value,
    actor: &TeamActor,
    execution: &forge_core::RunExecution,
) -> Result<()> {
    let thread = execution.thread_id.as_deref().ok_or_else(conflict)?;
    let turn = execution.turn_id.as_deref().ok_or_else(conflict)?;
    if execution.connection.worker_id != actor.agent_id {
        return Err(denied("Execution belongs to another member"));
    }
    let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut stored = authority(&tx, snapshot, actor)?;
    let binding = json!({"connection_id":execution.connection.connection_id,"thread_id":thread,"turn_id":turn});
    let mut sessions = stored.spec["runtime_sessions"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    if !sessions.contains(&binding) {
        sessions.push(binding);
        stored.spec["runtime_sessions"] = json!(sessions);
        stored.spec["runtime_completion"] = Value::Null;
        // Binding metadata does not change claim counters or the task result.
        tx.execute(
            "UPDATE tasks SET spec=?1 WHERE id=?2",
            params![encode(&stored.spec), stored.state.id],
        )?;
    }
    tx.commit()?;
    Ok(())
}

pub(crate) fn messages(c: &Connection, run: &str, key: &str) -> Result<Vec<Value>> {
    let raw: String = c
        .query_row(
            "SELECT spec FROM tasks WHERE run_id=?1 AND key=?2",
            params![run, key],
            |r| r.get(0),
        )
        .optional()?
        .ok_or_else(missing)?;
    let spec = decode(raw)?;
    let bindings = spec["runtime_sessions"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    if bindings.is_empty() {
        return Ok(Vec::new());
    }
    let mut query=c.prepare("SELECT m.rowid,m.payload FROM forge_runtime_messages m WHERE m.run_id=?1 AND m.kind='item/completed' AND json_extract(m.payload,'$.item.type') IN ('agentMessage','dynamicToolCall','mcpToolCall','commandExecution') AND EXISTS(SELECT 1 FROM json_each(?2) b WHERE json_extract(b.value,'$.connection_id')=m.connection_id AND json_extract(b.value,'$.thread_id')=COALESCE(json_extract(m.payload,'$.threadId'),json_extract(m.payload,'$.thread_id')) AND json_extract(b.value,'$.turn_id')=COALESCE(json_extract(m.payload,'$.turnId'),json_extract(m.payload,'$.turn_id'))) ORDER BY m.rowid")?;
    let items=query.query_map(params![run,encode(&json!(bindings))],|row| {
        let payload=decode(row.get(1)?)?;
        let item=&payload["item"];
        let assistant=item["type"]=="agentMessage";
        Ok(crate::python_sqlite::redact(json!({"index":row.get::<_,i64>(0)?,"role":if assistant{"assistant"}else{"tool"},"content":if assistant{item["text"].clone()}else{item.get("content").or_else(||item.get("aggregatedOutput")).or_else(||item.get("result")).cloned().unwrap_or(Value::Null)},"calls":[],"item_id":item["id"],"tool":item.get("tool").or_else(||item.get("command")).cloned().unwrap_or(Value::Null)})))
    })?.collect::<std::result::Result<Vec<_>,_>>()?;
    Ok(items)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projection_omits_claims_and_filters_connection_thread_and_turn() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(crate::product_store::PRODUCT_SCHEMA)
            .unwrap();
        c.execute_batch("CREATE TABLE forge_runtime_messages(run_id TEXT,connection_id TEXT,sequence INTEGER,kind TEXT,payload TEXT,created_at TEXT)").unwrap();
        let state = Task::new("run:task".into(), "run".into()).unwrap();
        let spec = json!({"claim_state":{"version":state.version,"claim":{"token":"PRIVATE-CLAIM"}},"runtime_sessions":[{"connection_id":"connection-a","thread_id":"thread-a","turn_id":"turn-a"}],"description":"task instruction","secret":"PRIVATE-SPEC"});
        c.execute("INSERT INTO tasks VALUES('run:task','run','task','autonomous','worker-a','Task','{}','done','[]','{}','','now')",[]).unwrap();
        c.execute(
            "UPDATE tasks SET spec=?1,result=?2",
            params![
                encode(&spec),
                encode(&json!({"summary":"Complete","nested":{"api_key":"PRIVATE-RESULT"}}))
            ],
        )
        .unwrap();
        for (seq, run, connection, thread, turn, text) in [
            (1, "run", "connection-a", "thread-a", "turn-a", "own output"),
            (
                2,
                "run",
                "connection-b",
                "thread-a",
                "turn-a",
                "other member",
            ),
            (
                3,
                "run",
                "connection-a",
                "thread-b",
                "turn-a",
                "other thread",
            ),
            (4, "run", "connection-a", "thread-a", "turn-b", "other task"),
            (
                5,
                "other-run",
                "connection-a",
                "thread-a",
                "turn-a",
                "other run",
            ),
        ] {
            c.execute("INSERT INTO forge_runtime_messages VALUES(?1,?2,?3,'item/completed',?4,'now')",params![run,connection,seq,encode(&json!({"threadId":thread,"turnId":turn,"item":{"id":format!("i-{seq}"),"type":"agentMessage","text":text}}))]).unwrap();
        }
        let view = tasks(&c, "run").unwrap();
        assert_eq!(view[0]["agent_id"], "worker-a");
        assert_eq!(view[0]["status"], "done");
        assert!(!json!(view).to_string().contains("PRIVATE"));
        assert!(view[0]["spec"].get("claim_state").is_none());
        let output = messages(&c, "run", "task").unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0]["content"], "own output");
        c.execute("UPDATE tasks SET spec='{}'", []).unwrap();
        assert!(messages(&c, "run", "task").unwrap().is_empty());
    }
}
