//! A host-boundary spike, not a deployable control service.
//! No outgoing message escapes before the host acknowledges durable Ready storage.
use protobuf::Message as PbMessage;
use raft::{eraftpb::{Entry, HardState, Message, Snapshot}, storage::MemStorage,
           Config, RawNode, ReadOnlyOption, Ready};
use serde::{Deserialize, Serialize};

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Stable {
    pub hard_state: Vec<u8>,
    pub entries: Vec<Vec<u8>>,
    pub snapshot: Vec<u8>,
    pub applied: u64,
}

#[derive(Serialize)]
pub struct PersistBatch {
    pub sequence: u64,
    pub hard_state: Option<Vec<u8>>,
    pub entries: Vec<Vec<u8>>,
    pub snapshot: Option<Vec<u8>>,
}

#[derive(Serialize)]
pub struct AppliedBatch {
    pub messages: Vec<Vec<u8>>,
    pub committed: Vec<Vec<u8>>,
    pub commit_index: Option<u64>,
}

fn encode<T: PbMessage>(m: &T) -> Vec<u8> { m.write_to_bytes().expect("protobuf encode") }
fn entries(bytes: &[Vec<u8>]) -> Result<Vec<Entry>, String> {
    bytes.iter().map(|b| Entry::parse_from_bytes(b).map_err(|e| e.to_string())).collect()
}

pub struct Core {
    node: RawNode<MemStorage>,
    pending: Option<Ready>,
    sequence: u64,
    awaiting_apply: Option<u64>,
}

impl Core {
    pub fn restore(id: u64, stable: Stable) -> Result<Self, String> {
        if ![1, 2, 3].contains(&id) { return Err("static member required".into()); }
        let store = MemStorage::new_with_conf_state((vec![1, 2, 3], vec![]));
        {
            let mut w = store.wl();
            if !stable.snapshot.is_empty() {
                w.apply_snapshot(Snapshot::parse_from_bytes(&stable.snapshot).map_err(|e| e.to_string())?)
                    .map_err(|e| e.to_string())?;
            }
            w.append(&entries(&stable.entries)?).map_err(|e| e.to_string())?;
            if !stable.hard_state.is_empty() {
                w.set_hardstate(HardState::parse_from_bytes(&stable.hard_state).map_err(|e| e.to_string())?);
            }
        }
        let config = Config { id, applied: stable.applied, election_tick: 10, heartbeat_tick: 1,
            check_quorum: true, pre_vote: true, read_only_option: ReadOnlyOption::Safe,
            max_size_per_msg: 16 * 1024, max_committed_size_per_ready: 16 * 1024,
            max_uncommitted_size: 64 * 1024, max_inflight_msgs: 4, ..Default::default() };
        config.validate().map_err(|e| e.to_string())?;
        let logger = slog::Logger::root(slog::Discard, slog::o!());
        Ok(Self { node: RawNode::new(&config, store, &logger).map_err(|e| e.to_string())?,
                  pending: None, sequence: 0, awaiting_apply: None })
    }

    pub fn step(&mut self, wire: &[u8]) -> Result<(), String> {
        if self.pending.is_some() || self.awaiting_apply.is_some() { return Err("finish outstanding Ready first".into()); }
        if wire.len() > 32 * 1024 { return Err("bounded message required".into()); }
        let m = Message::parse_from_bytes(wire).map_err(|e| e.to_string())?;
        if ![1, 2, 3].contains(&m.from) || m.to != self.node.raft.id {
            return Err("wrong static member".into());
        }
        self.node.step(m).map_err(|e| e.to_string())
    }

    pub fn tick(&mut self) -> Result<(), String> {
        if self.pending.is_some() || self.awaiting_apply.is_some() { return Err("finish outstanding Ready first".into()); }
        self.node.tick();
        Ok(())
    }

    pub fn ready(&mut self) -> Result<Option<PersistBatch>, String> {
        if self.pending.is_some() || self.awaiting_apply.is_some() { return Err("Ready already outstanding".into()); }
        if !self.node.has_ready() { return Ok(None); }
        let rd = self.node.ready();
        self.sequence += 1;
        let batch = PersistBatch { sequence: self.sequence, hard_state: rd.hs().map(encode),
            entries: rd.entries().iter().map(encode).collect(),
            snapshot: if rd.snapshot().is_empty() { None } else { Some(encode(rd.snapshot())) } };
        self.pending = Some(rd);
        Ok(Some(batch))
    }

    /// Call ONLY after the host has committed and synced the complete PersistBatch.
    pub fn persisted(&mut self, sequence: u64) -> Result<AppliedBatch, String> {
        if sequence != self.sequence || self.pending.is_none() { return Err("wrong Ready receipt".into()); }
        let mut rd = self.pending.take().unwrap();
        {
            let mut w = self.node.mut_store().wl();
            if !rd.snapshot().is_empty() { w.apply_snapshot(rd.snapshot().clone()).map_err(|e| e.to_string())?; }
            w.append(rd.entries()).map_err(|e| e.to_string())?;
            if let Some(hs) = rd.hs() { w.set_hardstate(hs.clone()); }
        }
        let mut messages = rd.take_messages();
        messages.extend(rd.take_persisted_messages());
        let mut committed = rd.take_committed_entries();
        let mut light = self.node.advance_append(rd);
        messages.extend(light.take_messages());
        committed.extend(light.take_committed_entries());
        self.awaiting_apply = committed.last().map(|e| e.index);
        Ok(AppliedBatch { messages: messages.iter().map(encode).collect(),
            committed: committed.iter().map(encode).collect(), commit_index: light.commit_index() })
    }

    /// Host must first durably apply state machine effects, dedup receipts and applied index.
    pub fn applied(&mut self, index: u64) -> Result<(), String> {
        if self.awaiting_apply != Some(index) { return Err("wrong applied receipt".into()); }
        self.node.advance_apply_to(index);
        self.awaiting_apply = None;
        Ok(())
    }
}

#[cfg(target_arch = "wasm32")]
mod wasm {
    use super::*;
    use wasm_bindgen::prelude::*;
    fn js(e: impl ToString) -> JsValue { JsValue::from_str(&e.to_string()) }
    #[wasm_bindgen]
    pub struct Voter(Core);
    #[wasm_bindgen]
    impl Voter {
        #[wasm_bindgen(constructor)]
        pub fn new(id: u32, stable_json: &str) -> Result<Voter, JsValue> {
            let stable = serde_json::from_str(stable_json).map_err(js)?;
            Core::restore(id as u64, stable).map(Voter).map_err(js)
        }
        pub fn step(&mut self, wire: &[u8]) -> Result<(), JsValue> { self.0.step(wire).map_err(js) }
        pub fn tick(&mut self) -> Result<(), JsValue> { self.0.tick().map_err(js) }
        pub fn ready(&mut self) -> Result<String, JsValue> {
            serde_json::to_string(&self.0.ready().map_err(js)?).map_err(js)
        }
        pub fn persisted(&mut self, sequence: u64) -> Result<String, JsValue> {
            serde_json::to_string(&self.0.persisted(sequence).map_err(js)?).map_err(js)
        }
        pub fn applied(&mut self, index: u64) -> Result<(), JsValue> { self.0.applied(index).map_err(js) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use raft::eraftpb::MessageType;

    // Same real RawNode used by the WASM export; this is not a mock Raft voter.
    #[test]
    fn durable_vote_survives_reconstruction() {
        let mut first = Core::restore(3, Stable::default()).unwrap();
        let mut vote = Message::default();
        vote.set_msg_type(MessageType::MsgRequestVote);
        vote.from = 1; vote.to = 3; vote.term = 7;
        first.step(&encode(&vote)).unwrap();
        let rd = first.ready().unwrap().unwrap();
        assert!(first.step(&encode(&vote)).is_err());
        assert!(first.persisted(rd.sequence + 1).is_err());
        let stable = Stable { hard_state: rd.hard_state.clone().unwrap(), ..Default::default() };
        // File-backed stable receipt: write+fsync+rename+directory fsync before acknowledgement.
        let path = std::env::temp_dir().join(format!("amita-raft-vote-{}.json", std::process::id()));
        let tmp = path.with_extension("tmp");
        use std::io::Write;
        let mut file = std::fs::File::create(&tmp).unwrap();
        file.write_all(&serde_json::to_vec(&stable).unwrap()).unwrap();
        file.sync_all().unwrap();
        std::fs::rename(&tmp, &path).unwrap();
        std::fs::File::open(path.parent().unwrap()).unwrap().sync_all().unwrap();
        let released = first.persisted(rd.sequence).unwrap();
        assert!(released.messages.iter().map(|m| Message::parse_from_bytes(m).unwrap())
            .any(|m| m.get_msg_type() == MessageType::MsgRequestVoteResponse && !m.reject));
        drop(first);
        let mut restored = Core::restore(3, serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap()).unwrap();
        vote.from = 2;
        restored.step(&encode(&vote)).unwrap();
        let rd = restored.ready().unwrap().unwrap();
        let released = restored.persisted(rd.sequence).unwrap();
        assert!(released.messages.iter().map(|m| Message::parse_from_bytes(m).unwrap())
            .any(|m| m.get_msg_type() == MessageType::MsgRequestVoteResponse && m.reject));
        std::fs::remove_file(path).unwrap();
    }
}
