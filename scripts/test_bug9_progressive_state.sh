#!/usr/bin/env bash
# Inspect a migrated work package known to have changed status in Jira.
set -euo pipefail
if [[ $# != 1 || ! $1 =~ ^[1-9][0-9]*$ ]]; then
    echo "Usage: J2O_OPENPROJECT_CONTAINER=<container> $0 <work-package-id>" >&2
    exit 2
fi
: "${J2O_OPENPROJECT_CONTAINER:?Set the OpenProject container to inspect}"
docker exec -i -e J2O_AUDIT_WORK_PACKAGE_ID="$1" "$J2O_OPENPROJECT_CONTAINER" bundle exec rails runner - <<'RUBY'
package = WorkPackage.find(Integer(ENV.fetch("J2O_AUDIT_WORK_PACKAGE_ID"), 10))
changes = package.journals.order(:version).filter_map do |journal|
  status = journal.data&.status_id
  [journal.version, status] if status
end
changes.each { |version, status| puts "Version #{version}: status #{status}" }
abort "Expected at least two historical statuses" unless changes.map(&:last).uniq.size > 1
RUBY
