plugins {
    // 注意：AGP 9.0 起 Kotlin 支持已内置，不再需要 org.jetbrains.kotlin.android 插件
    // （加了会直接报错）。Compose 编译器插件仍然保留。
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "cn.beaconmfg.app"
    compileSdk = 37

    defaultConfig {
        applicationId = "cn.beaconmfg.app"
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "0.1.0-p1"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    // AGP 9.x 的 Kotlin DSL：kotlinOptions{} 已移除，改用 kotlin.compilerOptions{}
    kotlin {
        compilerOptions {
            jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
            freeCompilerArgs.add("-opt-in=androidx.compose.material3.ExperimentalMaterial3Api")
        }
    }

    packaging {
        resources {
            // 指纹分片是 jsonl，不压缩能省一次解压 IO；索引 JSON 走默认压缩
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }

    // 内置 4.7MB 指纹 + 索引，放进 assets
    androidResources {
        noCompress += listOf(".jsonl")
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.security.crypto)

    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)

    implementation(libs.okhttp)
    implementation(libs.okhttp.sse)
    implementation(libs.kotlinx.coroutines.android)

    // 检索内核对拍测试（需要真机/模拟器）：./gradlew connectedAndroidTest
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.androidx.test.core)
    androidTestImplementation(libs.androidx.test.runner)
}
