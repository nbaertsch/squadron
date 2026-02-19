// Squadron — Azure Container Apps infrastructure
//
// Provisions:
//   1. Log Analytics workspace (required by Container App Environment)
//   2. Storage Account + Azure Files share (persistent config + state)
//   3. Container App Environment (with storage mount)
//   4. Container App (single container, pulls from GHCR)
//
// Persistent storage:
//   - .squadron/ config is synced to Azure Files by GitHub Actions
//   - SQLite DBs (registry, activity) persist across restarts
//   - Worktrees remain ephemeral (/tmp)
//
// Deploy:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file infra/main.bicep \
//     --parameters appName=my-squadron \
//                  ghcrImage=ghcr.io/owner/repo:latest \
//                  githubAppId=12345 \
//                  githubInstallationId=67890 \
//                  githubPrivateKey='<pem-contents>' \
//                  githubWebhookSecret='<secret>'

targetScope = 'resourceGroup'

// ── Parameters ──────────────────────────────────────────────────────────────

@description('Base name for all resources (lowercase, no special chars)')
@minLength(3)
@maxLength(24)
param appName string

@description('GHCR image URI (e.g. ghcr.io/nbaertsch/squadron:latest)')
param ghcrImage string

@description('Azure region (default: resource group location)')
param location string = resourceGroup().location

@description('Container CPU cores')
param cpuCores string = '1.0'

@description('Container memory (e.g. 2Gi)')
param memorySize string = '2Gi'

@description('Minimum replicas (0 = scale to zero)')
param minReplicas int = 1

@description('Maximum replicas')
param maxReplicas int = 1

@description('Revision suffix (set to a unique value to force a new revision)')
param revisionSuffix string = ''

// GitHub App credentials
@secure()
@description('GitHub App ID')
param githubAppId string

@secure()
@description('GitHub App private key (PEM contents)')
param githubPrivateKey string

@secure()
@description('GitHub Installation ID')
param githubInstallationId string

@secure()
@description('GitHub webhook secret')
param githubWebhookSecret string

@secure()
@description('Copilot token for headless auth (optional)')
param copilotGithubToken string = ''

@secure()
@description('Dashboard API key for authentication (optional, leave empty to disable auth)')
param dashboardApiKey string = ''

@description('GitHub repo URL to clone at startup (e.g. https://github.com/owner/repo)')
param repoUrl string = ''

@description('Branch to clone (default: main)')
param defaultBranch string = 'main'

// ── Log Analytics ───────────────────────────────────────────────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ── Storage Account + File Share ────────────────────────────────────────────

// Storage account name must be globally unique, lowercase, 3-24 chars
var storageAccountName = '${replace(appName, '-', '')}stor'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: 'squadron-data'
  properties: {
    shareQuota: 5 // 5 GB — plenty for config + SQLite DBs
  }
}

// ── Container App Environment ───────────────────────────────────────────────

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// Link storage to environment
resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'squadron-storage'
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: fileShare.name
      accessMode: 'ReadWrite'
    }
  }
}

// ── Container App ───────────────────────────────────────────────────────────

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      secrets: concat([
        { name: 'github-app-id', value: githubAppId }
        { name: 'github-private-key', value: githubPrivateKey }
        { name: 'github-installation-id', value: githubInstallationId }
        { name: 'github-webhook-secret', value: githubWebhookSecret }
        { name: 'copilot-github-token', value: copilotGithubToken }
      ], empty(dashboardApiKey) ? [] : [
        { name: 'dashboard-api-key', value: dashboardApiKey }
      ])
    }
    template: {
      containers: [
        {
          name: 'squadron'
          image: ghcrImage
          resources: {
            cpu: json(cpuCores)
            memory: memorySize
          }
          env: concat([
            { name: 'GITHUB_APP_ID', secretRef: 'github-app-id' }
            { name: 'GITHUB_PRIVATE_KEY', secretRef: 'github-private-key' }
            { name: 'GITHUB_INSTALLATION_ID', secretRef: 'github-installation-id' }
            { name: 'GITHUB_WEBHOOK_SECRET', secretRef: 'github-webhook-secret' }
            { name: 'COPILOT_GITHUB_TOKEN', secretRef: 'copilot-github-token' }
            { name: 'SQUADRON_REPO_URL', value: repoUrl }
            { name: 'SQUADRON_DEFAULT_BRANCH', value: defaultBranch }
            { name: 'SQUADRON_WORKTREE_DIR', value: '/tmp/squadron-worktrees' }
            // Data dir stays ephemeral (SQLite WAL doesn't work on SMB/Azure Files)
            { name: 'SQUADRON_DATA_DIR', value: '/tmp/squadron-data' }
            // Config dir on persistent mount (synced by GitHub Actions)
            { name: 'SQUADRON_CONFIG_DIR', value: '/mnt/squadron-data/.squadron' }
          ], empty(dashboardApiKey) ? [] : [
            { name: 'SQUADRON_DASHBOARD_API_KEY', secretRef: 'dashboard-api-key' }
          ])
          command: ['squadron', 'serve']
          args: ['--repo-root', '/tmp/squadron-repo', '--host', '0.0.0.0', '--port', '8000']
          volumeMounts: [
            {
              volumeName: 'squadron-data'
              mountPath: '/mnt/squadron-data'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'squadron-data'
          storageName: envStorage.name
          storageType: 'AzureFile'
        }
      ]
      revisionSuffix: revisionSuffix
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────────

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output webhookUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}/webhook'
output appName string = containerApp.name
output resourceGroup string = resourceGroup().name
output storageAccountName string = storageAccount.name
