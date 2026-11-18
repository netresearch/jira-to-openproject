import com.atlassian.jira.component.ComponentAccessor
import com.atlassian.jira.issue.fields.CustomField
import com.onresolve.scriptrunner.runner.rest.common.CustomEndpointDelegate
import groovy.json.JsonBuilder
import groovy.transform.BaseScript

import javax.ws.rs.core.MultivaluedMap
import javax.ws.rs.core.Response

@BaseScript CustomEndpointDelegate delegate

delegate.search(httpMethod: "GET", groups: ["jira-users"]) { MultivaluedMap queryParams ->

    def searchService = ComponentAccessor.getComponent(com.atlassian.jira.bc.issue.search.SearchService)
    def user = ComponentAccessor.jiraAuthenticationContext.loggedInUser
    def customFieldManager = ComponentAccessor.customFieldManager
    def baseUrl = ComponentAccessor.applicationProperties.getString("jira.baseurl")

    def jql = (queryParams.getFirst("jql") ?: "").toString()
    def startAt = (queryParams.getFirst("startAt") ?: "0") as int
    def maxResults = Math.min((queryParams.getFirst("maxResults") ?: "50") as int, 200) // Reduced max limit to 200

    // Get specific fields if provided, otherwise use a default set
    def fieldParam = queryParams.getFirst("fields")
    def requestedFields = fieldParam ? fieldParam.split(",").collect { it.trim() } : []

    def parseResult = searchService.parseQuery(user, jql)
    if (!parseResult.valid) {
        return Response.status(400).entity([
            error: "Invalid JQL",
            messages: parseResult.errors
        ]).build()
    }

    def query = parseResult.query

    // Use the pager to paginate results directly from the search service
    def pager = new com.atlassian.jira.web.bean.PagerFilter(startAt, maxResults)
    def results = searchService.search(user, query, pager)
    def issues = results.getResults()

    def coreFields = [
        summary: { issue -> issue.summary },
        description: { issue -> issue.description },
        created: { issue -> issue.created },
        updated: { issue -> issue.updated },
        duedate: { issue -> issue.dueDate },
        resolution: { issue -> issue.resolution?.id },
        reporter: { issue -> issue.reporter?.id },
        assignee: { issue -> issue.assignee?.id },
        status: { issue -> [id: issue.status?.id, name: issue.status?.name] },
        issuetype: { issue -> [id: issue.issueType?.id, name: issue.issueType?.name] },
        project: { issue -> [id: issue.projectObject.id, key: issue.projectObject.key, name: issue.projectObject.name] },
        priority: { issue -> issue.priority?.id },
        labels: { issue -> issue.labels as List },
        fixVersions: { issue -> issue.fixVersions?.collect { it.id } },
        affectedVersions: { issue -> issue.affectedVersions?.collect { it.id } },
        components: { issue -> issue.components?.collect { it.id } },
    ].findAll { name, reader -> requestedFields.isEmpty() || requestedFields.contains(name) }
    def requestedCustomFields = requestedFields.findAll { it.startsWith("customfield_") }
        .collect { customFieldManager.getCustomFieldObject(it) }
        .findAll { it != null }

    def issueData = issues.collect { issue ->
        def fields = [self: "${baseUrl}/rest/api/2/issue/${issue.key}"]
        coreFields.each { name, reader -> fields[name] = reader(issue) }
        def customFields = requestedFields.isEmpty() ? customFieldManager.getCustomFieldObjects(issue) : requestedCustomFields
        customFields.each { CustomField cf -> fields[cf.id] = cf.getValue(issue)?.toString() }

        return [
            id    : issue.id,
            key   : issue.key,
            fields: fields
        ]
    }

    def responseJson = new JsonBuilder([
        startAt   : startAt,
        maxResults: maxResults,
        total     : results.total,
        issues    : issueData
    ]).toPrettyString()

    return Response.ok(responseJson).build()
}
