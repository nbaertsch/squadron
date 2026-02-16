// Squadron — Azure Container Apps infrastructure
//
// Provisions:
//   1. Log Analytics workspace (required by Container App Environment)
//   2. Container App Environment
//   3. Container App (single container, pulls from GHCR)
//
// All state is ephemeral (container-local /tmp). The container clones the
// repo at startup, stores SQLite DB at /tmp/squadron-data, and creates
// worktrees at /tmp/squadron-worktrees.
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
      secrets: [
        { name: 'github-app-id', value: githubAppId }
        { name: 'github-private-key', value: githubPrivateKey }
        { name: 'github-installation-id', value: githubInstallationId }
        { name: 'github-webhook-secret', value: githubWebhookSecret }
        { name: 'copilot-github-token', value: copilotGithubToken }
      ]
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
          env: [
            { name: 'GITHUB_APP_ID', secretRef: 'github-app-id' }
            { name: 'GITHUB_PRIVATE_KEY', secretRef: 'github-private-key' }
            { name: 'GITHUB_INSTALLATION_ID', secretRef: 'github-installation-id' }
            { name: 'GITHUB_WEBHOOK_SECRET', secretRef: 'github-webhook-secret' }
            { name: 'COPILOT_GITHUB_TOKEN', secretRef: 'copilot-github-token' }
            { name: 'SQUADRON_REPO_URL', value: repoUrl }
            { name: 'SQUADRON_DEFAULT_BRANCH', value: defaultBranch }
            { name: 'SQUADRON_WORKTREE_DIR', value: '/tmp/squadron-worktrees' }
            { name: 'SQUADRON_DATA_DIR', value: '/tmp/squadron-data' }
          ]
          command: ['squadron', 'serve']
          args: ['--repo-root', '/tmp/squadron-repo', '--host', '0.0.0.0', '--port', '8000']
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
