// KubeSight CI/CD — https://github.com/eliemedawar/Kubesight
//
// Jenkins job setup (one-time):
//   1. New Item → Pipeline
//   2. Pipeline → Definition: "Pipeline script from SCM"
//   3. SCM: Git → https://github.com/eliemedawar/Kubesight.git
//      Branch: */master  |  Script Path: Jenkinsfile
//   4. Build Triggers → GitHub hook trigger for GITScm polling (optional)
//   5. Agent needs: Docker CLI (docker run / docker build)
//
// Optional plugins (not required): Timestamper, Docker Pipeline, JUnit

pipeline {
    agent any

    options {
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '25'))
        timeout(time: 45, unit: 'MINUTES')
    }

    parameters {
        booleanParam(
            name: 'RUN_POSTGRES_TESTS',
            defaultValue: false,
            description: 'Run backend tests against PostgreSQL (starts docker-compose.test.yml service).'
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
        GIT_REPO = 'https://github.com/eliemedawar/Kubesight.git'
        GIT_BRANCH = 'master'
        BACKEND_IMAGE_NAME = 'kubesight-backend'
        FRONTEND_IMAGE_NAME = 'kubesight-frontend'
        K8S_REAL_MODE = 'false'
        JWT_SECRET_KEY = 'ci-test-secret-do-not-use-in-production'
    }

    stages {
        stage('Checkout') {
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
                        expression { !params.RUN_POSTGRES_TESTS }
                    }
                    steps {
                        sh '''
                            docker run --rm \
                                -v "$WORKSPACE:/workspace" \
                                -w /workspace/backend \
                                -e K8S_REAL_MODE=false \
                                -e JWT_SECRET_KEY=ci-test-secret-do-not-use-in-production \
                                python:3.12-slim \
                                sh -c 'pip install --no-cache-dir -r requirements.txt && python -m pytest tests --junitxml=/workspace/test-results/backend-junit.xml -q'
                        '''
                    }
                    post {
                        always {
                            junit allowEmptyResults: true, testResults: 'test-results/backend-junit.xml'
                        }
                    }
                }

                stage('Backend (PostgreSQL)') {
                    when {
                        expression { params.RUN_POSTGRES_TESTS }
                    }
                    steps {
                        sh '''
                            if [ ! -f docker-compose.test.yml ]; then
                                echo "docker-compose.test.yml not found — falling back to SQLite tests."
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
                            sh '''
                                if [ -f docker-compose.test.yml ]; then
                                    docker compose -f docker-compose.test.yml down -v || true
                                fi
                            '''
                        }
                    }
                }

                stage('Frontend') {
                    steps {
                        sh '''
                            docker run --rm \
                                -v "$WORKSPACE:/workspace" \
                                -w /workspace/frontend \
                                node:20-alpine \
                                sh -c 'npm ci && npm test && npm run build'
                        '''
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
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'github-kubesight-credentials',
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
            echo "KubeSight pipeline succeeded — ${env.GIT_REPO} @ ${env.EFFECTIVE_TAG}"
        }
        failure {
            echo 'KubeSight pipeline failed — inspect stage logs above.'
        }
    }
}
