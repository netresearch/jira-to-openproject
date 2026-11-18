import com.atlassian.jira.component.ComponentAccessor
import com.atlassian.jira.issue.fields.CustomField
import com.atlassian.jira.issue.customfields.option.Option
import com.atlassian.jira.issue.fields.config.FieldConfig
import com.onresolve.scriptrunner.runner.rest.common.CustomEndpointDelegate
import groovy.json.JsonBuilder
import groovy.transform.BaseScript

import javax.ws.rs.core.MultivaluedMap
import javax.ws.rs.core.Response

@BaseScript CustomEndpointDelegate delegate

delegate.getAllCustomFieldsWithOptions(httpMethod: "GET", groups: ["jira-administrators"]) { MultivaluedMap queryParams ->
    def customFieldManager = ComponentAccessor.getCustomFieldManager()
    def optionsManager = ComponentAccessor.getOptionsManager()

    def customFields = customFieldManager.getCustomFieldObjects().collectEntries { CustomField cf ->
        def fieldTypeKey = cf.customFieldType.key

        def configs = cf.configurationSchemes.collectMany { scheme -> scheme.configs.values().toList() }
        def allOptions = configs.collectMany { FieldConfig fieldConfig -> optionsManager.getOptions(fieldConfig) ?: [] }
            .collect { Option option -> [id: option.optionId, value: option.value] }
            .unique()

        [(cf.id): [
            id: cf.id,
            name: cf.name,
            custom: true,
            clauseNames: cf.clauseNames,
            schema: [
                type: cf.customFieldType.name,
                custom: fieldTypeKey,
                customId: cf.idAsLong
            ],
            options: allOptions
        ]]
    }

    return Response.ok(new JsonBuilder(customFields).toPrettyString()).build()
}
