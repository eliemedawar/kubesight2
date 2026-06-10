// KubeSight CI/CD — https://github.com/eliemedawar/Kubesight
//
// Plugins needed: Pipeline, Git
//
// One-time agent setup (Jenkins in Docker):
//   docker exec -u root jenkins bash -c "apt-get update && apt-get install -y python3 python3-pip python3-venv nodejs npm docker.io"
//   docker exec -u root jenkins usermod -aG docker jenkins
//   docker exec -u root jenkins chmod 666 /var/run/docker.sock
//   docker restart jenkins
//
// Tests run directly on the Jenkins agent (not docker run) because nested
// volume mounts from Jenkins-in-Docker do not expose the workspace to child containers.
// Docker is only required when BUILD_DOCKER / PUSH_IMAGES / DEPLOY_TO_K8S is enabled.
//
// Job: SCM https://github.com/eliemedawar/Kubesight.git  branch */master
//      Credentials: github-creds  |  Script Path: Jenkinsfile

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
            description: 'Run backend tests against PostgreSQL (docker-compose.test.yml).'
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
            steps {
                checkout scm
                sh '''
                    mkdir -p test-results

                    if ! command -v python3 >/dev/null 2>&1; then
                        echo "ERROR: python3 not found on Jenkins agent."
                        echo "Run: docker exec -u root jenkins apt-get install -y python3 python3-pip python3-venv"
                        exit 1
                    fi
                    if ! command -v npm >/dev/null 2>&1; then
                        echo "ERROR: npm not found on Jenkins agent."
                        echo "Run: docker exec -u root jenkins apt-get install -y nodejs npm"
                        exit 1
                    fi
                '''
                script {
                    env.EFFECTIVE_TAG = params.IMAGE_TAG?.trim() ?: env.BUILD_NUMBER
                    String registry = params.DOCKER_REGISTRY?.trim()
                    env.BACKEND_IMAGE = registry ? "${registry}/${env.BACKEND_IMAGE_NAME}" : env.BACKEND_IMAGE_NAME
                    env.FRONTEND_IMAGE = registry ? "${registry}/${env.FRONTEND_IMAGE_NAME}" : env.FRONTEND_IMAGE_NAME

                    if (params.BUILD_DOCKER || params.PUSH_IMAGES || params.DEPLOY_TO_K8S) {
                        sh '''
                            command -v docker >/dev/null 2>&1 || { echo "ERROR: docker required for image build/deploy stages."; exit 1; }
                            docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not reachable."; exit 1; }
                        '''
                    }
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
                        dir('backend') {
                            sh '''
                                python3 -m venv .ci-venv
                                . .ci-venv/bin/activate
                                pip install --no-cache-dir -r requirements.txt
                                python -m pytest tests \
                                    --junitxml=../test-results/backend-junit.xml \
                                    -q
                            '''
                        }
                    }
                }

                stage('Backend (PostgreSQL)') {
                    when {
                        expression { params.RUN_POSTGRES_TESTS }
                    }
                    steps {
                        sh '''
                            set -e
                            if [ ! -f docker-compose.test.yml ]; then
                                echo "docker-compose.test.yml not found — falling back to SQLite."
                                cd backend
                                python3 -m venv .ci-venv
                                . .ci-venv/bin/activate
                                pip install --no-cache-dir -r requirements.txt
                                python -m pytest tests --junitxml=../test-results/backend-junit.xml -q
                                exit 0
                            fi

                            command -v docker >/dev/null 2>&1 || { echo "ERROR: docker required for PostgreSQL tests."; exit 1; }
                            docker compose -f docker-compose.test.yml up -d --wait postgres-test
                            cd backend
                            python3 -m venv .ci-venv
                            . .ci-venv/bin/activate
                            pip install --no-cache-dir -r requirements.txt
                            export TEST_DATABASE_URL="postgresql://kubesight_test:kubesight_test@host.docker.internal:5433/kubesight_test"
                            python -m pytest tests --junitxml=../test-results/backend-junit.xml -q
                        '''
                    }
                    post {
                        always {
                            sh 'docker compose -f docker-compose.test.yml down -v || true'
                        }
                    }
                }

                stage('Frontend') {
                    steps {
                        dir('frontend') {
                            sh '''
                                if [ -f package-lock.json ]; then
                                    npm ci
                                else
                                    npm install
                                fi
                                npm test
                                npm run build
                            '''
                        }
                    }
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: 'test-results/*.xml', allowEmptyArchive: true
                    archiveArtifacts artifacts: 'frontend/dist/**', allowEmptyArchive: true
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
