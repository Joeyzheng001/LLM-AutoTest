// 对应模块 2（CODE）/ 模块 3（REV）：Java 多仓库扫描与接口逆向还原服务入口。
package com.llmautotest.parser;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

/** java-parser 服务入口。 */
@SpringBootApplication
@ConfigurationPropertiesScan
public class ParserApplication {

    public static void main(String[] args) {
        SpringApplication.run(ParserApplication.class, args);
    }
}
