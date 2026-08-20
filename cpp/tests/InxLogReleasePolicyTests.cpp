#include <core/log/InxLog.h>

#include <cstdlib>
#include <vector>

using namespace infernux;

int main()
{
    InxLog &log = InxLog::GetInstance();
    log.SetLogLevel(LOG_DEBUG);

    std::vector<LogLevel> receivedLevels;
    const size_t sinkId = log.AddSink([&receivedLevels](LogLevel level, const char *, int, const std::string &, bool) {
        receivedLevels.push_back(level);
    });

    int evaluatedNonErrorArgument = 0;
    INXLOG_DEBUG(++evaluatedNonErrorArgument);
    INXLOG_INFO(++evaluatedNonErrorArgument);
    INXLOG_WARN(++evaluatedNonErrorArgument);
    log.Log(LOG_DEBUG, __FILE__, __LINE__, "direct debug call");
    log.Log(LOG_INFO, __FILE__, __LINE__, "direct info call");
    log.Log(LOG_WARN, __FILE__, __LINE__, "direct warning call");
    INXLOG_ERROR("release policy error probe");

    log.RemoveSink(sinkId);

    if (evaluatedNonErrorArgument != 0)
        return EXIT_FAILURE;
    if (receivedLevels.size() != 1 || receivedLevels.front() != LOG_ERROR)
        return EXIT_FAILURE;
    return EXIT_SUCCESS;
}
