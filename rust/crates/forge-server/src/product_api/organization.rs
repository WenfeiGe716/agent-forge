use super::*;

fn array<'a>(v: &'a Value, key: &str, max: usize) -> Result<&'a [Value]> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(&[]),
        Some(Value::Array(a)) if a.len() <= max => Ok(a),
        _ => Err(bad(format!("Invalid {key} array"))),
    }
}
fn integer(v: &Value, key: &str, min: u64, max: u64) -> Result<u64> {
    v[key]
        .as_u64()
        .filter(|n| *n >= min && *n <= max)
        .ok_or_else(|| bad(format!("Invalid {key}")))
}
fn position(p: &Value) -> Result<()> {
    number(p, "x", -12., 12.)?;
    number(p, "z", -8., 8.)?;
    integer(p, "floor", 1, 10)?;
    Ok(())
}
fn coordinate(v: &Value, key: &str) -> f64 {
    v[key].as_f64().unwrap_or(0.)
}
pub(super) fn validate(v: &Value) -> Result<()> {
    fields(
        v,
        &[
            "layout_version",
            "positions",
            "floors",
            "teams",
            "desks",
            "description",
        ],
    )?;
    let layout_version = v
        .get("layout_version")
        .map(|_| integer(v, "layout_version", 1, 2))
        .transpose()?
        .unwrap_or(1);
    let positions = object(&v["positions"])?;
    if positions.len() > 300 {
        return Err(bad("At most 300 member positions"));
    }
    let floors = array(v, "floors", 10)?;
    let teams = array(v, "teams", 100)?;
    let desks = array(v, "desks", 300)?;
    let explicit = v.get("desks").is_some_and(|d| !d.is_null());
    if !explicit && floors.is_empty() && positions.is_empty() {
        return Err(bad("Legacy office layouts require a member position"));
    }
    if !explicit && !teams.is_empty() {
        return Err(bad("Office teams require explicit desks"));
    }
    if floors.is_empty()
        && (!desks.is_empty() || !teams.is_empty() || layout_version == 2 && !positions.is_empty())
    {
        return Err(bad("Teams and desks require department floors"));
    }
    let mut floor_ids = BTreeSet::new();
    let mut departments = BTreeSet::new();
    let mut zones = BTreeMap::new();
    for floor in floors {
        fields(floor, &["floor", "name", "department", "zones"])?;
        let fid = integer(floor, "floor", 1, 10)?;
        let department = string(floor, "department", 1, 80)?;
        string(floor, "name", 1, 80)?;
        if !floor_ids.insert(fid)
            || !departments.insert(department.to_lowercase())
            || department.trim() != department
        {
            return Err(bad("Floor IDs and department names must be unique"));
        }
        let areas = array(floor, "zones", 20)?;
        if areas.is_empty() {
            return Err(bad("Each floor requires zones"));
        }
        let mut kinds = BTreeSet::new();
        for area in areas {
            fields(
                area,
                &["id", "name", "kind", "x", "z", "width", "depth", "color"],
            )?;
            let id = string(area, "id", 1, 48)?;
            string(area, "name", 1, 80)?;
            let kind = area["kind"].as_str().unwrap_or("work");
            if !["work", "meeting", "common", "lounge", "restroom", "pantry"].contains(&kind) {
                return Err(bad("Invalid office zone kind"));
            }
            kinds.insert(kind);
            if zones.insert(id, (fid, area)).is_some() {
                return Err(bad("Zone identifiers must be globally unique"));
            }
            for (k, lo, hi) in [
                ("x", -12., 12.),
                ("z", -8., 8.),
                ("width", 0.00001, 24.),
                ("depth", 0.00001, 16.),
            ] {
                number(area, k, lo, hi)?;
            }
            if coordinate(area, "x").abs() + coordinate(area, "width") / 2. > 12. + 1e-9
                || coordinate(area, "z").abs() + coordinate(area, "depth") / 2. > 8. + 1e-9
            {
                return Err(bad("Office zone extends beyond its floor"));
            }
            if layout_version == 2
                && ["meeting", "lounge", "restroom", "pantry"].contains(&kind)
                && (coordinate(area, "width") < (if kind == "meeting" { 4. } else { 3. })
                    || coordinate(area, "depth") < 3.)
            {
                return Err(bad("Shared office zone is too small"));
            }
        }
        if !kinds.contains("work")
            || layout_version == 2
                && ["meeting", "restroom", "pantry"]
                    .iter()
                    .any(|k| !kinds.contains(k))
        {
            return Err(bad("Office floor lacks required work or shared zones"));
        }
        for (i, a) in areas.iter().enumerate() {
            for b in &areas[i + 1..] {
                if (coordinate(a, "x") - coordinate(b, "x")).abs()
                    < (coordinate(a, "width") + coordinate(b, "width")) / 2. - 1e-9
                    && (coordinate(a, "z") - coordinate(b, "z")).abs()
                        < (coordinate(a, "depth") + coordinate(b, "depth")) / 2. - 1e-9
                {
                    return Err(bad("Office zones overlap"));
                }
            }
        }
    }
    let seats: Vec<_> = if explicit {
        desks.iter().collect()
    } else {
        positions.values().collect()
    };
    let mut desk_ids = BTreeSet::new();
    let mut seated = BTreeSet::new();
    let mut derived = Map::new();
    for seat in &seats {
        position(seat)?;
        if explicit {
            fields(
                seat,
                &[
                    "id", "name", "x", "z", "floor", "zone", "team_id", "agent_id",
                ],
            )?;
            if !desk_ids.insert(string(seat, "id", 1, 64)?) {
                return Err(bad("Desk IDs must be unique"));
            }
            string(seat, "name", 1, 80)?;
            if let Some(aid) = seat["agent_id"].as_str() {
                if !seated.insert(aid) {
                    return Err(bad("A member can occupy one desk"));
                }
                derived.insert(
                    aid.into(),
                    json!({"x":seat["x"],"z":seat["z"],"floor":seat["floor"],"zone":seat["zone"]}),
                );
            }
        }
        if !floors.is_empty() {
            let zid = seat["zone"].as_str().unwrap_or("");
            let (fid, zone) = zones
                .get(zid)
                .ok_or_else(|| bad("Desk zone does not exist"))?;
            if seat["floor"] != *fid || zone["kind"].as_str().unwrap_or("work") != "work" {
                return Err(bad("Desk must be inside a work zone on its floor"));
            }
            if coordinate(seat, "x") - 1.4
                < coordinate(zone, "x") - coordinate(zone, "width") / 2. - 1e-9
                || coordinate(seat, "x") + 1.4
                    > coordinate(zone, "x") + coordinate(zone, "width") / 2. + 1e-9
                || coordinate(seat, "z") - 0.6
                    < coordinate(zone, "z") - coordinate(zone, "depth") / 2. - 1e-9
                || coordinate(seat, "z") + 1.45
                    > coordinate(zone, "z") + coordinate(zone, "depth") / 2. + 1e-9
            {
                return Err(bad("Desk and chair must fit fully inside their work zone"));
            }
        }
    }
    if explicit && Value::Object(derived) != v["positions"] {
        return Err(bad("Member positions must exactly match occupied desks"));
    }
    for (i, a) in seats.iter().enumerate() {
        for b in &seats[i + 1..] {
            if a["floor"] == b["floor"]
                && (coordinate(a, "x") - coordinate(b, "x")).abs() < 2.8 - 1e-9
                && (coordinate(a, "z") - coordinate(b, "z")).abs() < 2.05 - 1e-9
            {
                return Err(bad("Office desks overlap"));
            }
        }
    }
    let mut team_ids = BTreeSet::new();
    let mut team_zones = BTreeSet::new();
    let mut members = BTreeSet::new();
    for team in teams {
        fields(
            team,
            &["id", "name", "floor", "zone_ids", "headcount", "member_ids"],
        )?;
        let id = string(team, "id", 1, 64)?;
        let fid = integer(team, "floor", 1, 10)?;
        string(team, "name", 1, 80)?;
        if !team_ids.insert(id) || !floor_ids.contains(&fid) {
            return Err(bad("Invalid team ID or floor"));
        }
        let areas = strings(team, "zone_ids", 20)?;
        if areas.is_empty() {
            return Err(bad("Team needs a work zone"));
        }
        for zid in areas {
            let (zfloor, zone) = zones
                .get(zid.as_str())
                .ok_or_else(|| bad("Team zone does not exist"))?;
            if *zfloor != fid
                || zone["kind"].as_str().unwrap_or("work") != "work"
                || !team_zones.insert(zid)
            {
                return Err(bad("Teams must own distinct work zones on their floor"));
            }
        }
        let ids = strings(team, "member_ids", 300)?;
        let headcount = integer(team, "headcount", 0, 300)?;
        if ids.len() > headcount as usize
            || headcount as usize > desks.iter().filter(|d| d["team_id"] == id).count()
        {
            return Err(bad("Team headcount must fit assigned desks"));
        }
        for aid in ids {
            if !members.insert(aid) {
                return Err(bad("Members cannot belong to multiple teams"));
            }
        }
    }
    for desk in desks {
        let team = desk["team_id"]
            .as_str()
            .map(|id| {
                teams
                    .iter()
                    .find(|t| t["id"] == id)
                    .ok_or_else(|| bad("Desk references unknown team"))
            })
            .transpose()?;
        if let Some(t) = team {
            if desk["floor"] != t["floor"]
                || !t["zone_ids"]
                    .as_array()
                    .is_some_and(|z| z.contains(&desk["zone"]))
            {
                return Err(bad("Desk is outside its team's work zones"));
            }
        }
        if let Some(aid) = desk["agent_id"].as_str() {
            if team.is_none_or(|t| {
                !t["member_ids"]
                    .as_array()
                    .is_some_and(|ids| ids.contains(&json!(aid)))
            }) {
                return Err(bad("Occupied desk must belong to the member's team"));
            }
        }
    }
    if explicit
        && seated
            .into_iter()
            .map(str::to_owned)
            .collect::<BTreeSet<_>>()
            != members
    {
        return Err(bad("Every team member requires one desk"));
    }
    Ok(())
}
