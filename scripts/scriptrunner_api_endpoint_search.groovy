import com.atlassian.jira.component.ComponentAccessor
import com.atlassian.jira.issue.fields.CustomField
import com.onresolve.scriptrunner.runner.rest.common.CustomEndpointDelegate
import groovy.json.JsonBuilder
import groovy.transform.BaseScript

import javax.ws.rs.core.MultivaluedMap
import javax.ws.rs.core.Response

@BaseScript CustomEndpointDelegate delegate

search(httpMethod: "GET", groups: ["jira-users"]) { MultivaluedMap queryParams ->

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

    // Cache custom fields by ID to avoid lookups for each issue
    def customFieldsById = [:]

    def issueData = issues.collect { issue ->
        def fields = [:]

        // Add 'self' link like the REST API
        fields["self"] = "${baseUrl}/rest/api/2/issue/${issue.key}"

        // Only include requested fields or core fields if no specific fields requested
        if (requestedFields.isEmpty() || requestedFields.contains("summary")) fields["summary"] = issue.summary
        if (requestedFields.isEmpty() || requestedFields.contains("description")) fields["description"] = issue.description
        if (requestedFields.isEmpty() || requestedFields.contains("created")) fields["created"] = issue.created
        if (requestedFields.isEmpty() || requestedFields.contains("updated")) fields["updated"] = issue.updated
        if (requestedFields.isEmpty() || requestedFields.contains("duedate")) fields["duedate"] = issue.dueDate
        if (requestedFields.isEmpty() || requestedFields.contains("resolution")) fields["resolution"] = issue.resolution?.id
        if (requestedFields.isEmpty() || requestedFields.contains("reporter")) fields["reporter"] = issue.reporter?.id
        if (requestedFields.isEmpty() || requestedFields.contains("assignee")) fields["assignee"] = issue.assignee?.id
        if (requestedFields.isEmpty() || requestedFields.contains("status")) fields["status"] = [id: issue.status?.id, name: issue.status?.name]
        if (requestedFields.isEmpty() || requestedFields.contains("issuetype")) fields["issuetype"] = [id: issue.issueType?.id, name: issue.issueType?.name]
        if (requestedFields.isEmpty() || requestedFields.contains("project")) fields["project"] = [id: issue.projectObject.id, key: issue.projectObject.key, name: issue.projectObject.name]
        if (requestedFields.isEmpty() || requestedFields.contains("priority")) fields["priority"] = issue.priority?.id
        if (requestedFields.isEmpty() || requestedFields.contains("labels")) fields["labels"] = issue.labels as List
        if (requestedFields.isEmpty() || requestedFields.contains("fixVersions")) fields["fixVersions"] = issue.fixVersions?.collect { it.id }
        if (requestedFields.isEmpty() || requestedFields.contains("affectedVersions")) fields["affectedVersions"] = issue.affectedVersions?.collect { it.id }
        if (requestedFields.isEmpty() || requestedFields.contains("components")) fields["components"] = issue.components?.collect { it.id }

        // Process custom fields - only if requested or if no specific fields are requested
        // Only get custom fields that match requested fields or all if no specific fields requested
        if (requestedFields.isEmpty() || requestedFields.any { it.startsWith("customfield_") }) {
            def relevantCustomFields = requestedFields.isEmpty() ?
                customFieldManager.getCustomFieldObjects(issue) :
                requestedFields.findAll { it.startsWith("customfield_") }.collect { cfId ->
                    // Cache custom field objects to avoid repeated lookups
                    if (!customFieldsById.containsKey(cfId)) {
                        customFieldsById[cfId] = customFieldManager.getCustomFieldObject(cfId)
                    }
                    return customFieldsById[cfId]
                }.findAll { it != null }

            relevantCustomFields.each { CustomField cf ->
                def value = cf.getValue(issue)
                fields[cf.id] = value?.toString()
            }
        }

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
