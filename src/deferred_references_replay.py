# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Replay archived references with both target identities checked in the write transaction."""

from typing import Any

from src.application.components.relation_migration import RelationMigration

# All source values are staged as data, including the Jira URL and the issue keys.
_APPLY = """
data=input_data
begin
result=WorkPackage.transaction(requires_new:true) do
  rows=data.fetch('rows')
  work=WorkPackage.lock.where(id:rows.flat_map{|r|[r['source']['wp'],r['target']['wp']]}.uniq.sort).order(:id).index_by(&:id)
  projects=Project.lock.where(id:work.values.map(&:project_id).uniq.sort).order(:id).to_a
  raise 'Replay requires private cutover projects' if projects.any?(&:public?) || Member.where(project_id:projects.map(&:id)).exists?
  raise 'Work package module required' unless projects.all?{|p|p.module_enabled?(:work_package_tracking)}
  fields=%w[Key ID URL System].to_h{|n|[n,WorkPackageCustomField.where(name:"J2O Origin #{n}").sole.id]}
  changed=0
  rows.each do |r|
    source,target=%w[source target].map do |side|
      endpt=r.fetch(side);wp=work.fetch(endpt.fetch('wp'))
      expected={'Key'=>endpt['key'],'ID'=>endpt['id'],'System'=>'Jira','URL'=>data['jira']+'/browse/'+endpt['key']}
      raise 'Work package provenance mismatch' unless expected.all?{|k,v| wp.custom_values.where(custom_field_id:fields[k]).sole.value==v}
      wp
    end
    raise 'Self reference' if source==target
    if r['kind']=='epic'
      if source.parent_id.nil?
        source.parent=target;source.save!(touch:false);changed+=1
      end
      raise 'Existing parent differs' unless source.reload.parent_id==target.id
    else
      source,target=target,source if r['swap']
      kind=r.fetch('relation_type')
      edge=Relation.where(from_id:source.id,to_id:target.id,relation_type:kind)
      edge=edge.or(Relation.where(from_id:target.id,to_id:source.id,relation_type:kind)) if kind=='relates'
      if !edge.exists?
        Relation.create!(from:source,to:target,relation_type:kind);changed+=1
      end
      edge.sole
    end
  end
  {confirmed:rows.size,created:changed}
end
rescue StandardError
  result={error:"Target identity, privacy, or relation check failed"}
end
puts $j2o_start_marker, result.to_json, $j2o_end_marker
"""


def _endpoint(original: dict, mapped: dict) -> dict | None:
    entry = mapped.get(original["key"])
    if not entry or original["id"] is None:
        return None
    if entry[0] != original["id"]:
        msg = "Mapped Jira key belongs to a different immutable issue ID"
        raise ValueError(msg)
    return {**original, "wp": entry[1]}


def apply(data: dict, mapped: dict, client: Any) -> dict[str, int]:
    """Keep unresolved records encrypted. Materialize only unambiguous mapped references."""
    rows = []
    directions = RelationMigration.supported_directions()
    for ref in data["references"]:
        source = _endpoint(ref["source"], mapped)
        target = _endpoint(ref["target"], mapped)
        if not source or not target:
            continue
        row = {"source": source, "target": target, "kind": ref["kind"]}
        if ref["kind"] == "link":
            rule = directions.get((ref["type"]["name"].lower(), ref["direction"]))
            if rule is None:
                continue
            row.update(relation_type=rule[0], swap=rule[1])
        elif ref["kind"] != "epic" or ref["direct_parent"]:
            continue
        rows.append(row)
    result = {"held": len(data["references"]) - len(rows), "confirmed": 0, "created": 0}
    if rows:
        response = client.execute_script_with_data(_APPLY, {"jira": data["jira"], "rows": rows})
        counts = response.get("data") or {}
        created = counts.get("created")
        if (
            response.get("status") != "success"
            or counts.get("confirmed") != len(rows)
            or type(created) is not int
            or not 0 <= created <= len(rows)
        ):
            msg = "Incomplete target acknowledgement for deferred references"
            raise ValueError(msg)
        result.update(confirmed=counts["confirmed"], created=counts["created"])
    return result
