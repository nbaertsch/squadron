// Squadron — Azure Container Apps infrastructure
//
// Provisions:
//   1. Log Analytics workspace (required by Container App Environment)
//   2. Storage Account + Azure Files shares (persistent config + state + forensics)
//   3. Container App Environment (with storage mounts)
//   4. Container App (single container, pulls from GHCR)
//
// Persistent storage:
//   - .squadron/ config is synced to Azure Files by GitHub Actions
//   - SQLite DBs (registry, activity) persist across restarts
//   - Worktrees remain ephemeral (/tmp)
//   - Forensic evidence (abnormal-exit sandboxes) retained in /mnt/squadron-data/forensics
//
// Sandbox notes:
//   - Sandbox is DISABLED by default (sandboxEnabled = false).
//   - When enabled, the container requires CAP_SYS_ADMIN for Linux namespace
//     isolation (unshare).  Azure Container Apps supports this via securityContext.
//   - fuse-overlayfs and libseccomp2 are always installed in the image; they
//     are inert unless sandbox.enabled is true in .squadron/config.yaml.
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

// ── Sandbox parameters ───────────────────────────────────────────────────────

@description('''
Enable the sandboxed agent execution model (issue #85 / #97).
When true:
  - Adds CAP_SYS_ADMIN to the container for Linux namespace isolation (unshare).
  - Mounts the forensics file share at /mnt/squadron-data/forensics.
  - Sets SQUADRON_SANDBOX_ENABLED=true in the container environment.
Keep false (default) until sandbox code is merged and tested.
''')
param sandboxEnabled bool = false

@description('Forensic retention path inside the container (must match sandbox.retention_path in config.yaml)')
param sandboxRetentionPath string = '/mnt/squadron-data/forensics'

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

// ── Storage Account + File Shares ───────────────────────────────────────────

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

// Primary data share: .squadron config + SQLite DBs
resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: 'squadron-data'
  properties: {
    shareQuota: 5 // 5 GB — plenty for config + SQLite DBs
  }
}

// Forensics share: abnormal-exit sandbox worktrees retained for audit (1-day default)
// Always provisioned so it is available when sandbox is later enabled without re-deploying.
resource forensicsShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: 'squadron-forensics'
  properties: {
    shareQuota: 10 // 10 GB — accommodates up to ~10 retained worktree snapshots
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

// Primary data mount
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

// Forensics mount — always registered so it is available on first sandbox activation
resource envForensicsStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'squadron-forensics'
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: forensicsShare.name
      accessMode: 'ReadWrite'
    }
  }
}

// ── Container App ───────────────────────────────────────────────────────────

// Volumes: always include primary data; add forensics mount when sandbox is enabled
var baseVolumes = [
  {
    name: 'squadron-data'
    storageName: envStorage.name
    storageType: 'AzureFile'
  }
]

var forensicsVolume = {
  name: 'squadron-forensics'
  storageName: envForensicsStorage.name
  storageType: 'AzureFile'
}

// Volume mounts for the container
var baseVolumeMounts = [
  {
    volumeName: 'squadron-data'
    mountPath: '/mnt/squadron-data'
  }
]

var forensicsVolumeMount = {
  volumeName: 'squadron-forensics'
  mountPath: sandboxRetentionPath
}

// Sandbox env vars (only injected when sandbox is active to avoid confusing operators)
var sandboxEnvVars = sandboxEnabled ? [
  { name: 'SQUADRON_SANDBOX_ENABLED', value: 'true' }
  { name: 'SQUADRON_SANDBOX_RETENTION_PATH', value: sandboxRetentionPath }
] : []

// CAP_SYS_ADMIN is required by `unshare` for PID/net/mount namespace isolation.
// It is only added when sandboxEnabled = true; otherwise the container runs
// with its default (restricted) capability set.
var sandboxSecurityContext = sandboxEnabled ? {
  capabilities: {
    add: [
      'SYS_ADMIN'  // Required for unshare namespace isolation
    ]
  }
} : {}

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
          securityContext: sandboxSecurityContext
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
          ], sandboxEnvVars)
          command: ['squadron', 'serve']
          args: ['--repo-root', '/tmp/squadron-repo', '--host', '0.0.0.0', '--port', '8000']
          volumeMounts: concat(baseVolumeMounts, sandboxEnabled ? [forensicsVolumeMount] : [])
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
      volumes: concat(baseVolumes, sandboxEnabled ? [forensicsVolume] : [])
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
output forensicsShareName string = forensicsShare.name
