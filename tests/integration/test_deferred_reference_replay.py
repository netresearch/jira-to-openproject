# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Confirm actual 17.9.7 relation and parent writes in an isolated rollback transaction."""

import json

import pytest

from src.deferred_references_replay import apply
from tests.integration.test_openproject_agile_roundtrip import rails as rails
from tests.unit.test_deferred_references import sample

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_replay_uses_both_verified_endpoints_and_rechecks_native_records(rails):
    client, query = rails

    def submit(script, rows):
        value = query(f"input_data = JSON.parse({json.dumps(json.dumps(rows))}); {script}\nresult")
        return {"status": "success", "data": value}

    client.execute_script_with_data.side_effect = submit
    ids = query("""
      template=WorkPackage.first!
      projects=%w[one two].map{|n|Project.create!(name:"Deferred #{n}",identifier:"j2o-deferred-#{n}",workspace_type:"project",public:false)}
      specs=[['APP-1','10',0],['EXT-3','12',1],['EXT-4','13',1]]
      specs.map do |key,jid,pi|
        wp=WorkPackage.create!(project:projects[pi],type:template.type,status:template.status,priority:template.priority,
                               subject:'Deferred reference fixture',author:User.current)
        {'Key'=>key,'ID'=>jid,'URL'=>"https://jira.example/browse/#{key}",'System'=>'Jira'}.each do |n,v|
          cf=WorkPackageCustomField.find_or_create_by!(name:"J2O Origin #{n}"){|f|f.field_format='string'}
          CustomValue.create!(customized:wp,custom_field:cf,value:v)
        end
        wp.id
      end
    """)
    local, linked, epic = ids
    mapped = {"APP-1": ("10", local), "EXT-3": ("12", linked), "EXT-4": ("13", epic)}
    data = sample()
    assert apply(data, {}, client)["held"] == 2
    assert apply(data, mapped, client) == {"confirmed": 2, "created": 2, "held": 0}
    assert apply(data, mapped, client) == {"confirmed": 2, "created": 0, "held": 0}
    assert query(f"WorkPackage.find({local}).parent_id") == epic
    assert query(f"Relation.where(from_id:{linked},to_id:{local},relation_type:'blocks').count") == 1
    assert query(f"""
      token=Token::API.create!(user:User.current)
      begin
        cli=Rack::MockRequest.new(Rails.application)
        headers={{"HTTP_HOST"=>"localhost","HTTP_AUTHORIZATION"=>ActionController::HttpAuthentication::Basic.encode_credentials("apikey",token.plain_value)}}
        rid=Relation.where(from_id:{linked},to_id:{local},relation_type:'blocks').sole.id
        wp=cli.get("http://localhost/api/v3/work_packages/{local}",headers)
        rel=cli.get("http://localhost/api/v3/relations/#{{rid}}",headers)
        raise 'Unexpected API status' unless wp.status==200 && rel.status==200
        [JSON.parse(wp.body)["_links"]["parent"]["href"],JSON.parse(rel.body)["_links"]["from"]["href"],JSON.parse(rel.body)["_links"]["to"]["href"]]
      ensure;token.destroy!;end
    """) == [f"/api/v3/work_packages/{n}" for n in (epic, linked, local)]
    query(f"WorkPackage.find({epic}).project.update!(public:true);true")
    with pytest.raises(ValueError, match="Incomplete target"):
        apply(data, mapped, client)
