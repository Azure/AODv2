// Customer-side infrastructure for AODv2 auto-upload.
//
// Deployed by a customer admin: the role assignment requires Owner or User
// Access Administrator (decision D4). AODv2 itself creates nothing.
//
//   az deployment group create -g <rg> -f main.bicep \
//      -p vmPrincipalIds='["<vm-identity-principal-id>"]' quotaGb=50

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Globally unique name for the dedicated diagnostics storage account (D1).')
param storageAccountName string = 'aoddiag${uniqueString(resourceGroup().id)}'

@description('Container that receives diagnostic packages.')
param containerName string = 'aod-diagnostics'

@description('''Principal IDs granted upload rights. Pass the principal id of an
existing user-assigned identity to share one grant across a fleet, or a VM's
system-assigned identity to grant a single host. The identity itself is created
outside this template so an existing one can be reused.''')
param vmPrincipalIds array = []

@description('''Client id of the uploader identity, echoed back as an output so
setup.sh can configure hosts without looking it up again. Not used to deploy
anything.''')
param uploaderClientId string = ''

@description('Delete packages older than this. The free floor beneath the sweeper (D2).')
param retentionDays int = 30

@description('Size budget enforced by the sweeper. Not an Azure limit - Blob has none (D2).')
param quotaGb int = 50

@description('Sweep when usage passes this fraction of the quota.')
param highWaterFraction string = '0.9'

@description('Drain down to this fraction once sweeping.')
param lowWaterFraction string = '0.7'

@description('''Create the custom role and its assignments.
Set false when the deployer lacks Owner / User Access Administrator: everything
else is created and an admin can assign roles separately.''')
param deployRbac bool = true

@description('''Deploy the sweeper function that enforces the size quota.
Set false where the subscription has no quota for the Y1 (Consumption) SKU, or
where the function is not wanted: everything else still deploys and the
lifecycle rule still deletes by age, but --quota-gb is not enforced.''')
param deploySweeper bool = true

@description('''Create the Event Grid subscription.
Must be false on the first deployment: the subscription targets a function that
does not exist until the sweeper code is published, and Event Grid validates the
endpoint at creation time. setup.sh deploys, publishes, then re-deploys with this
set to true.''')
param deployEventGrid bool = false

var blobPrefix = 'aodv2/'
var functionAppName = 'aod-sweeper-${uniqueString(resourceGroup().id)}'
var hostingPlanName = 'aod-sweeper-plan-${uniqueString(resourceGroup().id)}'

// ---------------------------------------------------------------- storage

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // Entra-only: no account keys, so a client cannot hold a god credential (D4).
    allowSharedKeyAccess: false
    accessTier: 'Hot'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    // Soft delete and versioning keep deleted data billable, which makes the
    // sweeper delete more while freeing nothing (Constraints).
    deleteRetentionPolicy: {
      enabled: false
    }
    containerDeleteRetentionPolicy: {
      enabled: false
    }
    isVersioningEnabled: false
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Age-based floor. Cannot express a size limit - that is why the sweeper exists.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-old-diagnostics'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['${containerName}/${blobPrefix}']
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterCreationGreaterThan: retentionDays
                }
              }
            }
          }
        }
      ]
    }
  }
}

// ------------------------------------------------------------------- RBAC

// Built-in Storage Blob Data Contributor includes delete. Clients must not be
// able to delete other hosts' evidence (D2), so this grants write without it.
// Note: 'write' still permits overwriting an existing blob - see Constraints.
resource clientRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = if (deployRbac) {
  name: guid(resourceGroup().id, 'aod-blob-writer')
  properties: {
    roleName: 'AODv2 Diagnostic Uploader (${storageAccountName})'
    description: 'Create and write blobs, no delete. Used by AODv2 hosts.'
    type: 'CustomRole'
    assignableScopes: [storage.id]
    permissions: [
      {
        // Reading container properties is a management action, not a data one.
        // The preflight check needs it to tell "no container" apart from "no
        // permission"; it does not expose any blob content.
        actions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/read'
        ]
        notActions: []
        // No blobs/read: a host must not be able to read another host's
        // evidence, which is the same reason D1 rejected a fleet-wide account.
        // Put Blob, Put Block and Put Block List all map to blobs/write.
        dataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'
        ]
        notDataActions: []
      }
    ]
  }
}

// Scoped to the container, not the account.
resource clientAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in (deployRbac ? vmPrincipalIds : []): {
    name: guid(container.id, principalId, 'aod-uploader')
    scope: container
    properties: {
      roleDefinitionId: clientRole.id
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// ------------------------------------------------------- sweeper function

resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = if (deploySweeper) {
  name: hostingPlanName
  location: location
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = if (deploySweeper) {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      appSettings: [
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'AOD_STORAGE_ACCOUNT'
          value: storage.name
        }
        {
          name: 'AOD_CONTAINER'
          value: containerName
        }
        {
          name: 'AOD_PREFIX'
          value: blobPrefix
        }
        {
          name: 'AOD_ENDPOINT_SUFFIX'
          value: environment().suffixes.storage
        }
        {
          name: 'AOD_QUOTA_BYTES'
          value: string(quotaGb * 1024 * 1024 * 1024)
        }
        {
          name: 'AOD_HIGH_WATER'
          value: highWaterFraction
        }
        {
          name: 'AOD_LOW_WATER'
          value: lowWaterFraction
        }
      ]
    }
  }
}

// The sweeper is the only principal allowed to delete.
resource sweeperRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRbac && deploySweeper) {
  name: guid(container.id, functionApp.id, 'sweeper')
  scope: container
  properties: {
    // Storage Blob Data Contributor
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
    )
    principalId: functionApp!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Wakes the sweeper on every upload, so enforcement is event-driven (D2).
resource systemTopic 'Microsoft.EventGrid/systemTopics@2023-12-15-preview' = if (deploySweeper) {
  name: 'aod-${storageAccountName}'
  location: location
  properties: {
    source: storage.id
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

resource blobCreatedSubscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2023-12-15-preview' = if (deploySweeper && deployEventGrid) {
  parent: systemTopic
  name: 'aod-blob-created'
  properties: {
    destination: {
      endpointType: 'AzureFunction'
      properties: {
        resourceId: '${functionApp.id}/functions/on_blob_created'
      }
    }
    filter: {
      includedEventTypes: ['Microsoft.Storage.BlobCreated']
      subjectBeginsWith: '/blobServices/default/containers/${containerName}/blobs/${blobPrefix}'
    }
    retryPolicy: {
      maxDeliveryAttempts: 10
      eventTimeToLiveInMinutes: 1440
    }
  }
}

output storageAccount string = storage.name
output containerName string = containerName
output functionAppName string = deploySweeper ? functionApp!.name : ''
output sweeperPrincipalId string = deploySweeper ? functionApp!.identity.principalId : ''
output rbacDeployed bool = deployRbac

// Empty when no shared identity is in use, so setup.sh can branch on it.
output uploaderIdentityClientId string = uploaderClientId

@description('Paste into AODv2 config.yaml under upload.destination')
output aodUploadConfig object = {
  account: storage.name
  container: containerName
  endpoint_suffix: environment().suffixes.storage
  prefix: 'aodv2/v1'
}
