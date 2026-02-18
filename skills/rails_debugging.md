# Ruby on Rails / vconnect Debugging

When investigating bugs in Rails services (vconnect):

## Observability
- Check New Relic APM: error rate, transaction traces, slow queries
- Look at recent deployments in New Relic deployment markers

## Common Issues
- N+1 queries (fix with includes/eager_load)
- Missing database indexes (check db/schema.rb)
- Background job failures (Sidekiq/Resque — check dead letter queue)
- Memory bloat from large ActiveRecord result sets (use find_each/in_batches)
- Race conditions in concurrent request handling
- Missing model validations leading to bad data
- Gem version incompatibilities after bundle update

## Code Structure
- Models: app/models/
- Controllers: app/controllers/
- Services: app/services/
- Jobs: app/jobs/ or app/workers/
- Migrations: db/migrate/
- Config: config/

## Fix Guidelines
- Follow existing Ruby style (check .rubocop.yml)
- Add or update RSpec tests
- Include bug ID in PR description and branch name
