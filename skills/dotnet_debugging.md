# .NET / OXO.APIs Debugging

When investigating bugs in .NET microservices (OXO.APIs):

## Observability
- Check Grafana dashboards for the specific service: request rate, error rate, latency
- Look for recent deployment markers that correlate with the bug report time

## Common Issues
- NullReferenceException in async code paths
- Entity Framework Core N+1 queries (check SQL query logs in Grafana)
- Middleware pipeline ordering issues (check Startup.cs / Program.cs)
- Dependency injection lifetime mismatches (Scoped vs Singleton)
- Connection pool exhaustion (check active connection count metrics)
- Deadlocks from .Result or .Wait() on async code
- Missing null checks on nullable reference types

## Code Structure
- Controllers: /Controllers/
- Services: /Services/
- Data access: /Repositories/ or /Data/
- Config: appsettings.json, Startup.cs or Program.cs

## Fix Guidelines
- Follow existing code style (check .editorconfig if present)
- Add or update unit tests for the fix
- Include bug ID in PR description and branch name
