# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Verify that an existing work package field follows types created after it."""

import pytest

from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService
from tests.integration.test_openproject_agile_roundtrip import rails as rails

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_ensured_field_gains_missing_types(rails):
    client, query = rails
    fields = OpenProjectCustomFieldService(client)
    field_id = fields.ensure_custom_field("J2O Late Type Test")["id"]
    query(f"""
      cf = WorkPackageCustomField.find({field_id})
      if cf.respond_to?(:type_variant_ids)
        cf.type_variant_ids = TypeVariant.default_variant.ids.first(1)
      else
        cf.type_ids = Type.ids.first(1)
      end
      cf.save!
    """)

    assert fields.ensure_custom_field("J2O Late Type Test")["id"] == field_id
    links = query(f"""
      cf = WorkPackageCustomField.find({field_id})
      if cf.respond_to?(:type_variant_ids)
        {{linked: cf.type_variant_ids.sort, expected: TypeVariant.default_variant.ids.sort}}
      else
        {{linked: cf.type_ids.sort, expected: Type.ids.sort}}
      end
    """)
    assert len(links["expected"]) > 1
    assert links["linked"] == links["expected"]


def test_ensured_field_keeps_a_link_outside_the_default_variants(rails):
    client, query = rails
    fields = OpenProjectCustomFieldService(client)
    field_id = fields.ensure_custom_field("J2O Variant Test")["id"]
    state = query(f"""
      if defined?(TypeVariant) && WorkPackageCustomField.method_defined?(:type_variant_ids)
        cf = WorkPackageCustomField.find({field_id})
        template = TypeVariant.default_variant.first!
        other = template.dup
        other.assign_attributes(variant_name: "J2O Variant Test", is_default_variant: false,
          enabled_in_new_projects: false)
        other.save!
        cf.type_variant_ids = [other.id]
        {{supported: true, id: other.id, default: TypeVariant.default_variant.exists?(other.id)}}
      else
        {{supported: false}}
      end
    """)
    if not state["supported"]:
        pytest.skip("OpenProject without type variants")
    assert state["default"] is False

    fields.ensure_custom_field("J2O Variant Test")
    linked, missing = query(f"""
      ids = WorkPackageCustomField.find({field_id}).type_variant_ids
      [ids, TypeVariant.default_variant.ids - ids]
    """)
    assert state["id"] in linked
    assert missing == []
