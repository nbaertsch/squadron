// Squadron — Azure Container Instances infrastructure
//
// Provisions:
//   1. Log Analytics workspace (container diagnostics)
//   2. Storage Account + Azure Files share (persistent config + state)
//   3. Container Group (single container, pulls from GHCR, privileged mode)
//
// Why ACI instead of Container Apps:
//   ACI supports securityContext with Linux capabilities (CAP_NET_ADMIN,
//   CAP_SYS_ADMIN) required for the sandbox network namespace isolation
//   (MitM proxy). Container Apps runs on Hyper-V with no capability grants.
//
// Persistent storage:
//   - .squadron/ config is synced to Azure Files by GitHub Actions
//   - SQLite DBs (registry, activity) persist across restarts
//   - Worktrees remain ephemeral (/tmp)
//
// Networking:
//   ACI does not provide managed HTTPS ingress like Container Apps.
//   The container exposes port 8000 directly via a public IP with DNS label.
//   For production, front with Azure Application Gateway or a reverse proxy.
//
// Deploy:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file infra/main-aci.bicep \
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
param cpuCores int = 1

@description('Container memory in GB')
param memoryInGB int = 2

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

@description('GHCR username for image pull (typically github username or org)')
param ghcrUsername string = ''

@secure()
@description('GHCR password/PAT for image pull (needs read:packages scope)')
param ghcrPassword string = ''

@description('Restart policy: Always, OnFailure, Never')
@allowed(['Always', 'OnFailure', 'Never'])
param restartPolicy string = 'Always'

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

// ── Container Group (ACI) ───────────────────────────────────────────────────

// Build the environment variables array.
// ACI does not have secretRef — all values are passed as secureValue directly.
var baseEnvVars = [
  { name: 'GITHUB_APP_ID', secureValue: githubAppId }
  { name: 'GITHUB_PRIVATE_KEY', secureValue: githubPrivateKey }
  { name: 'GITHUB_INSTALLATION_ID', secureValue: githubInstallationId }
  { name: 'GITHUB_WEBHOOK_SECRET', secureValue: githubWebhookSecret }
  { name: 'COPILOT_GITHUB_TOKEN', secureValue: copilotGithubToken }
  { name: 'SQUADRON_REPO_URL', value: repoUrl }
  { name: 'SQUADRON_DEFAULT_BRANCH', value: defaultBranch }
  { name: 'SQUADRON_WORKTREE_DIR', value: '/tmp/squadron-worktrees' }
  // Data dir stays ephemeral (SQLite WAL doesn't work on SMB/Azure Files)
  { name: 'SQUADRON_DATA_DIR', value: '/tmp/squadron-data' }
  // Config dir on persistent mount (synced by GitHub Actions)
  { name: 'SQUADRON_CONFIG_DIR', value: '/mnt/squadron-data/.squadron' }
]

var dashboardEnvVars = empty(dashboardApiKey) ? [] : [
  { name: 'SQUADRON_DASHBOARD_API_KEY', secureValue: dashboardApiKey }
]

var envVars = concat(baseEnvVars, dashboardEnvVars)

// Image registry credentials (GHCR is private by default)
var imageRegistryCredentials = empty(ghcrUsername) ? [] : [
  {
    server: 'ghcr.io'
    username: ghcrUsername
    password: ghcrPassword
  }
]

resource containerGroup 'Microsoft.ContainerInstance/containerGroups@2023-05-01' = {
  name: appName
  location: location
  properties: {
    osType: 'Linux'
    restartPolicy: restartPolicy
    // Image pull credentials for GHCR
    imageRegistryCredentials: empty(imageRegistryCredentials) ? null : imageRegistryCredentials
    containers: [
      {
        name: 'squadron'
        properties: {
          image: ghcrImage
          resources: {
            requests: {
              cpu: cpuCores
              memoryInGB: memoryInGB
            }
          }
          environmentVariables: envVars
          command: ['squadron', 'serve', '--repo-root', '/tmp/squadron-repo', '--host', '0.0.0.0', '--port', '8000']
          ports: [
            {
              port: 8000
              protocol: 'TCP'
            }
          ]
          volumeMounts: [
            {
              name: 'squadron-data'
              mountPath: '/mnt/squadron-data'
            }
          ]
          livenessProbe: {
            httpGet: {
              path: '/health'
              port: 8000
            }
            initialDelaySeconds: 15
            periodSeconds: 30
            failureThreshold: 3
          }
          // ACI securityContext: grant CAP_NET_ADMIN + CAP_SYS_ADMIN for sandbox
          // network namespace isolation (MitM proxy, bridge, veth pairs).
          securityContext: {
            privileged: false
            capabilities: {
              add: [
                'NET_ADMIN'
                'SYS_ADMIN'
              ]
            }
          }
        }
      }
    ]
    volumes: [
      {
        name: 'squadron-data'
        azureFile: {
          shareName: fileShare.name
          storageAccountName: storageAccount.name
          storageAccountKey: storageAccount.listKeys().keys[0].value
        }
      }
    ]
    // Public IP with DNS label for external access
    ipAddress: {
      type: 'Public'
      dnsNameLabel: appName
      ports: [
        {
          port: 8000
          protocol: 'TCP'
        }
      ]
    }
    diagnostics: {
      logAnalytics: {
        workspaceId: logAnalytics.properties.customerId
        workspaceKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────────

output fqdn string = containerGroup.properties.ipAddress.fqdn
output webhookUrl string = 'http://${containerGroup.properties.ipAddress.fqdn}:8000/webhook'
output healthUrl string = 'http://${containerGroup.properties.ipAddress.fqdn}:8000/health'
output appName string = containerGroup.name
output resourceGroup string = resourceGroup().name
output storageAccountName string = storageAccount.name
output ipAddress string = containerGroup.properties.ipAddress.ip
