compile with all strategies using this command:


python3 compile_all.py \
  --input idoft_excluded_693.csv \
  --repo-column "Project URL" \
  --sha-column "SHA Detected" \
  --module-column "Module Path" \
  --test-column "Fully-Qualified Test Name (packageName.ClassName.methodName)" \
  --strategy-mode all \
  --java8-home /usr/lib/jvm/java-8-openjdk-amd64 \
  --java11-home /usr/lib/jvm/java-11-openjdk-amd64 \
  --java17-home /usr/lib/jvm/java-17-openjdk-amd64



The script tries this sequence:

Skip flags only

flags + Java 8
flags + Java 11
flags + Java 17

Change HTTP URLs to HTTPS

https + Java 8
https + Java 11
https + Java 17

Replace -SNAPSHOT versions

snapshot + Java 8
snapshot + Java 11
snapshot + Java 17

Apply both POM changes

https+snapshot + Java 8
https+snapshot + Java 11
https+snapshot + Java 17

This gives a maximum of 12 Maven attempts per subject.