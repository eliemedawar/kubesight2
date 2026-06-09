// KubeSight CI/CD — https://github.com/eliemedawar/Kubesight
//
// === Jenkins plugins (Manage Jenkins → Plugins) ===
//   Required:
//     - Pipeline
//     - Git
//     - Docker Pipeline          (docker { } agent blocks)
//     - JUnit                    (junit test reports)
//   Recommended:
//     - Timestamper              (timestamps in console)
//     - GitHub / GitHub Integration (webhook auto-build)
//
// === Jenkins agent ===
//   Docker must be available on the agent. If Jenkins runs in Docker, start it with:
//     -v /var/run/docker.sock:/var/run/docker.sock
//   and add the jenkins user to the docker group inside the container.
//
// === Job config ===
//   SCM: https://github.com/eliemedawar/Kubesight.git  branch */master
//   Credentials ID: github-creds
//   Script Path: Jenkinsfile
//   Build trigger: GitHub hook trigger for GITScm polling (optional)

pipeline {
    agent none

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '25'))
        timeout(time: 45, unit: 'MINUTES')
        githubProjectProperty(projectUrlStr: 'https://github.com/eliemedawar/Kubesight')
    }

    parameters {
        booleanParam(
            name: 'RUN_POSTGRES_TESTS',
            defaultValue: false,
            description: 'Run backend tests against PostgreSQL (requires Docker + docker-compose.test.yml).'
        )
        booleanParam(
            name: 'BUILD_DOCKER',
            defaultValue: false,
            description: 'Build backend and frontend Docker images after tests pass.'
        )
        booleanParam(
            name: 'PUSH_IMAGES',
            defaultValue: false,
            description: 'Push built images to DOCKER_REGISTRY (requires BUILD_DOCKER).'
        )
        booleanParam(
            name: 'DEPLOY_TO_K8S',
            defaultValue: false,
            description: 'Apply k8s manifests and roll out new images (requires BUILD_DOCKER).'
        )
        string(
            name: 'DOCKER_REGISTRY',
            defaultValue: 'ghcr.io/eliemedawar',
            description: 'Registry prefix, e.g. ghcr.io/eliemedawar (no trailing slash).'
        )
        string(
            name: 'IMAGE_TAG',
            defaultValue: '',
            description: 'Image tag (defaults to BUILD_NUMBER).'
        )
        string(
            name: 'K8S_NAMESPACE',
            defaultValue: 'kubesight',
            description: 'Kubernetes namespace for deployment.'
        )
    }

    environment {
        BACKEND_IMAGE_NAME = 'kubesight-backend'
        FRONTEND_IMAGE_NAME = 'kubesight-frontend'
        K8S_REAL_MODE = 'false'
        JWT_SECRET_KEY = 'ci-test-secret-do-not-use-in-production'
    }

    stages {
        stage('Prepare') {
            agent any
            steps {
                checkout scm
                sh 'mkdir -p test-results'
                script {
                    env.EFFECTIVE_TAG = params.IMAGE_TAG?.trim() ?: env.BUILD_NUMBER
                    String registry = params.DOCKER_REGISTRY?.trim()
                    env.BACKEND_IMAGE = registry ? "${registry}/${env.BACKEND_IMAGE_NAME}" : env.BACKEND_IMAGE_NAME
                    env.FRONTEND_IMAGE = registry ? "${registry}/${env.FRONTEND_IMAGE_NAME}" : env.FRONTEND_IMAGE_NAME
                }
            }
        }

        stage('Test') {
            parallel {
                stage('Backend (SQLite)') {
                    when {
                        beforeAgent true
                        expression { !params.RUN_POSTGRES_TESTS }
                    }
                    agent {
                        docker {
                            image 'python:3.12-slim'
                            reuseNode true
                        }
                    }
                    steps {
                        dir('backend') {
                            sh '''
                                pip install --no-cache-dir -r requirements.txt
                                python -m pytest tests \
                                    --junitxml=../test-results/backend-junit.xml \
                                    -q
                            '''
                        }
                    }
                    post {
                        always {
                            junit allowEmptyResults: true, testResults: 'test-results/backend-junit.xml'
                        }
                    }
                }

                stage('Backend (PostgreSQL)') {
                    when {
                        beforeAgent true
                        expression { params.RUN_POSTGRES_TESTS }
                    }
                    agent any
                    steps {
                        sh '''
                            set -e
                            if [ ! -f docker-compose.test.yml ]; then
                                echo "docker-compose.test.yml not found — falling back to SQLite."
                                docker run --rm \
                                    -v "$WORKSPACE:/workspace" \
                                    -w /workspace/backend \
                                    -e K8S_REAL_MODE=false \
                                    -e JWT_SECRET_KEY=ci-test-secret-do-not-use-in-production \
                                    python:3.12-slim \
                                    sh -c 'pip install --no-cache-dir -r requirements.txt && python -m pytest tests --junitxml=/workspace/test-results/backend-junit.xml -q'
                                exit 0
                            fi

                            docker compose -f docker-compose.test.yml up -d --wait postgres-test
                            docker run --rm \
                                --network host \
                                -v "$WORKSPACE:/workspace" \
                                -w /workspace/backend \
                                -e K8S_REAL_MODE=false \
                                -e JWT_SECRET_KEY=ci-test-secret-do-not-use-in-production \
                                -e TEST_DATABASE_URL=postgresql://kubesight_test:kubesight_test@localhost:5433/kubesight_test \
                                python:3.12-slim \
                                sh -c 'pip install --no-cache-dir -r requirements.txt && python -m pytest tests --junitxml=/workspace/test-results/backend-junit.xml -q'
                        '''
                    }
                    post {
                        always {
                            junit allowEmptyResults: true, testResults: 'test-results/backend-junit.xml'
                            sh 'docker compose -f docker-compose.test.yml down -v || true'
                        }
                    }
                }

                stage('Frontend') {
                    agent {
                        docker {
                            image 'node:20-alpine'
                            reuseNode true
                        }
                    }
                    steps {
                        dir('frontend') {
                            sh '''
                                npm ci
                                npm test
                                npm run build
                            '''
                        }
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'frontend/dist/**', fingerprint: true, allowEmptyArchive: true
                        }
                    }
                }
            }
        }

        stage('Build Docker Images') {
            when {
                expression { params.BUILD_DOCKER }
            }
            agent any
            parallel {
                stage('Backend Image') {
                    steps {
                        dir('backend') {
                            sh """
                                docker build \
                                    -t ${env.BACKEND_IMAGE}:${env.EFFECTIVE_TAG} \
                                    -t ${env.BACKEND_IMAGE}:latest \
                                    .
                            """
                        }
                    }
                }

                stage('Frontend Image') {
                    steps {
                        dir('frontend') {
                            sh """
                                docker build \
                                    -t ${env.FRONTEND_IMAGE}:${env.EFFECTIVE_TAG} \
                                    -t ${env.FRONTEND_IMAGE}:latest \
                                    .
                            """
                        }
                    }
                }
            }
        }

        stage('Push Images') {
            when {
                allOf {
                    expression { params.BUILD_DOCKER }
                    expression { params.PUSH_IMAGES }
                    expression { params.DOCKER_REGISTRY?.trim() }
                }
            }
            agent any
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'github-creds',
                    usernameVariable: 'REGISTRY_USER',
                    passwordVariable: 'REGISTRY_PASS'
                )]) {
                    sh """
                        echo "\$REGISTRY_PASS" | docker login ${params.DOCKER_REGISTRY} -u "\$REGISTRY_USER" --password-stdin
                        docker push ${env.BACKEND_IMAGE}:${env.EFFECTIVE_TAG}
                        docker push ${env.BACKEND_IMAGE}:latest
                        docker push ${env.FRONTEND_IMAGE}:${env.EFFECTIVE_TAG}
                        docker push ${env.FRONTEND_IMAGE}:latest
                    """
                }
            }
        }

        stage('Deploy to Kubernetes') {
            when {
                allOf {
                    expression { params.BUILD_DOCKER }
                    expression { params.DEPLOY_TO_K8S }
                }
            }
            agent any
            steps {
                sh """
                    kubectl apply -f k8s/namespace.yaml
                    kubectl apply -f k8s/configmap.yaml
                    kubectl apply -f k8s/secret.yaml
                    kubectl apply -f k8s/rbac.yaml
                    kubectl apply -f k8s/postgres-pvc.yaml
                    kubectl apply -f k8s/postgres-deployment.yaml
                    kubectl apply -f k8s/kubeconfig-pvc.yaml
                    kubectl apply -f k8s/backend-deployment.yaml
                    kubectl apply -f k8s/frontend-deployment.yaml
                    kubectl apply -f k8s/ingress.yaml

                    kubectl -n ${params.K8S_NAMESPACE} set image deployment/kubesight-backend \
                        backend=${env.BACKEND_IMAGE}:${env.EFFECTIVE_TAG}
                    kubectl -n ${params.K8S_NAMESPACE} set image deployment/kubesight-frontend \
                        frontend=${env.FRONTEND_IMAGE}:${env.EFFECTIVE_TAG}

                    kubectl -n ${params.K8S_NAMESPACE} rollout status deployment/kubesight-backend --timeout=180s
                    kubectl -n ${params.K8S_NAMESPACE} rollout status deployment/kubesight-frontend --timeout=180s
                """
            }
        }
    }

    post {
        success {
            echo "KubeSight pipeline succeeded (tag: ${env.EFFECTIVE_TAG})."
        }
        failure {
            echo 'KubeSight pipeline failed — inspect stage logs above.'
        }
    }
}
