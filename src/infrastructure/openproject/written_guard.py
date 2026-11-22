# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Rails helpers that keep J2O from replacing a value someone changed in OpenProject (ADR-013).

J2O records, per work package and attribute, the SHA-256 of the value it last wrote. The
digest is computed here, from the stored value, so the normalisation OpenProject applies on
save never looks like an edit. ``j2o_decide`` returns one of three states:

* ``:unchanged`` - the target already holds the wanted value; nothing is written.
* ``:write`` - the target holds what J2O wrote last, or a value J2O never wrote over and no
  one set (empty, or the field default), or the operator allowed overwriting.
* ``:changed`` - someone else changed the value; the row is skipped and reported.
"""

WRITTEN_GUARD = r"""
require "digest"
j2o_digest = ->(value) { Digest::SHA256.hexdigest(value.to_json) }
j2o_stored = lambda do |wp, attr|
  if attr == "description"
    WorkPackage.where(id: wp.id).pick(:description).to_s
  else
    id = Integer(attr.delete_prefix("customField"), 10)
    CustomValue.where(customized_type: "WorkPackage", customized_id: wp.id, custom_field_id: id)
               .pluck(:value).compact.reject(&:empty?).sort
  end
end
j2o_untouched = lambda do |wp, attr|
  return [""] if attr == "description"
  cf = CustomField.find(Integer(attr.delete_prefix("customField"), 10))
  [[], Array(cf.default_value).map(&:to_s).reject(&:empty?).sort].uniq
end
j2o_decide = lambda do |guard, before, wanted, untouched|
  next :unchanged if before == wanted
  next :write if guard["overwrite"] == true
  expect = guard["expect"]
  next :write if expect ? j2o_digest.call(before) == expect : untouched.include?(before)
  :changed
end
"""
